import math

import torch
from torch.nn import functional as F
from torch.utils import data as torch_data
from torch_scatter import scatter_add

from ogb import linkproppred

from torchdrug import core, tasks, metrics
from torchdrug.layers import functional
from torchdrug.core import Registry as R

from nbfnet import dataset


Evaluator = core.make_configurable(linkproppred.Evaluator)
Evaluator = R.register("ogb.linkproppred.Evaluator")(Evaluator)
setattr(linkproppred, "Evaluator", Evaluator)


@R.register("tasks.KnowledgeGraphCompletionExt")
class KnowledgeGraphCompletionExt(tasks.KnowledgeGraphCompletion, core.Configurable):

    def __init__(self, model, criterion="bce",
                 metric=("mr", "mrr", "hits@1", "hits@3", "hits@10", "1-to-1", "1-to-n", "n-to-1", "n-to-n"),
                 num_negative=128, margin=6, adversarial_temperature=0, strict_negative=True, filtered_ranking=True,
                 fact_ratio=None, sample_weight=True):
        super(KnowledgeGraphCompletionExt, self).__init__(
            model, criterion, metric, num_negative, margin, adversarial_temperature, strict_negative, filtered_ranking,
            fact_ratio, sample_weight)

    def preprocess(self, train_set, valid_set, test_set):
        super(KnowledgeGraphCompletionExt, self).preprocess(train_set, valid_set, test_set)

        degree_hr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
        degree_tr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
        for h, t, r in train_set:
            degree_hr[h, r] += 1
            degree_tr[t, r] += 1

        has_category = False
        for _metric in self.metric:
            if _metric in ["1-to-1", "1-to-n", "n-to-1", "n-to-n"]:
                has_category = True
        if has_category:
            is_to_one = degree_hr.sum(dim=0).float() / (degree_hr > 0).sum(dim=0) < 1.5
            is_one_to = degree_tr.sum(dim=0).float() / (degree_tr > 0).sum(dim=0) < 1.5
            self.register_buffer("is_one_to_one", is_one_to & is_to_one)
            self.register_buffer("is_one_to_many", is_one_to & ~is_to_one)
            self.register_buffer("is_many_to_one", ~is_one_to & is_to_one)
            self.register_buffer("is_many_to_many", ~is_one_to & ~is_to_one)
            assert self.is_one_to_one.sum() + self.is_one_to_many.sum() + \
                   self.is_many_to_one.sum() + self.is_many_to_many.sum() == self.num_relation
            assert (self.is_one_to_one | self.is_one_to_many | self.is_many_to_one | self.is_many_to_many).all()

    def target(self, batch):
        mask, target = super(KnowledgeGraphCompletionExt, self).target(batch)
        relation = batch[:, 2]
        # in case of GPU OOM
        return mask, target, relation.cpu()

    def evaluate(self, pred, target):
        mask, target, relation = target

        pos_pred = pred.gather(-1, target.unsqueeze(-1))
        if self.filtered_ranking:
            ranking = torch.sum((pos_pred <= pred) & mask, dim=-1) + 1
        else:
            ranking = torch.sum(pos_pred <= pred, dim=-1) + 1
            
        pred = pred.flatten()
        target = target.flatten()
        metric = {}
        for _metric in self.metric:
            if _metric == "mr":
                score = ranking.float().mean()
            elif _metric == "mrr":
                score = (1 / ranking.float()).mean()
            elif _metric.startswith("hits@"):
                threshold = int(_metric[5:])
                score = (ranking <= threshold).float().mean()
            elif _metric == "1-to-1":
                score = (1 / ranking[self.is_one_to_one[relation]].float()).mean()
                metric["1-to-1 tail"] = (1 / ranking[self.is_one_to_one[relation]].float()).mean(dim=0)[0]
                metric["1-to-1 head"] = (1 / ranking[self.is_one_to_one[relation]].float()).mean(dim=0)[1]
            elif _metric == "1-to-n":
                score = (1 / ranking[self.is_one_to_many[relation]].float()).mean()
                metric["1-to-n tail"] = (1 / ranking[self.is_one_to_many[relation]].float()).mean(dim=0)[0]
                metric["1-to-n head"] = (1 / ranking[self.is_one_to_many[relation]].float()).mean(dim=0)[1]
            elif _metric == "n-to-1":
                score = (1 / ranking[self.is_many_to_one[relation]].float()).mean()
                metric["n-to-1 tail"] = (1 / ranking[self.is_many_to_one[relation]].float()).mean(dim=0)[0]
                metric["n-to-1 head"] = (1 / ranking[self.is_many_to_one[relation]].float()).mean(dim=0)[1]
            elif _metric == "n-to-n":
                score = (1 / ranking[self.is_many_to_many[relation]].float()).mean()
                metric["n-to-n tail"] = (1 / ranking[self.is_many_to_many[relation]].float()).mean(dim=0)[0]
                metric["n-to-n head"] = (1 / ranking[self.is_many_to_many[relation]].float()).mean(dim=0)[1]
            else:
                raise ValueError("Unknown metric `%s`" % _metric)

            name = tasks._get_metric_name(_metric)
            metric[name] = score

        return metric


@R.register("tasks.LinkPrediction")
class LinkPrediction(tasks.Task, core.Configurable):

    _option_members = ["criterion", "metric"]

    def __init__(self, model, criterion="bce", metric=("auroc", "ap"), num_negative=128, strict_negative=True):
        super(LinkPrediction, self).__init__()
        self.model = model
        self.criterion = criterion
        self.metric = metric
        self.num_negative = num_negative
        self.strict_negative = strict_negative

    def preprocess(self, train_set, valid_set, test_set):
        if isinstance(train_set, torch_data.Subset):
            dataset = train_set.dataset
        else:
            dataset = train_set
        self.num_node = dataset.num_node
        train_mask = train_set.indices
        valid_mask = train_set.indices + valid_set.indices
        train_graph = dataset.graph.edge_mask(train_mask)
        valid_graph = dataset.graph.edge_mask(valid_mask)
        self.register_buffer("train_graph", train_graph.undirected())
        self.register_buffer("valid_graph", valid_graph.undirected())
        self.register_buffer("test_graph", dataset.graph.undirected())

    def forward(self, batch):
        all_loss = torch.tensor(0, dtype=torch.float32, device=self.device)
        metric = {}

        pred, target = self.predict_and_target(batch, all_loss, metric)
        metric.update(self.evaluate(pred, target))

        for criterion, weight in self.criterion.items():
            if criterion == "bce":
                loss = F.binary_cross_entropy_with_logits(pred, target, reduction="none")
                neg_weight = torch.ones_like(pred)
                neg_weight[:, 1:] = 1 / self.num_negative
                loss = (loss * neg_weight).sum(dim=-1) / neg_weight.sum(dim=-1)
            else:
                raise ValueError("Unknown criterion `%s`" % criterion)

            loss = loss.mean()
            name = tasks._get_criterion_name(criterion)
            metric[name] = loss
            all_loss += loss * weight

        return all_loss, metric

    @torch.no_grad()
    def _strict_negative(self, count, split="train"):
        graph = getattr(self, "%s_graph" % split)

        node_in = graph.edge_list[:, 0]
        degree_in = torch.bincount(node_in, minlength=self.num_node)
        prob = (graph.num_node - degree_in - 1).float()

        neg_h_index = functional.multinomial(prob, count, replacement=True)
        any = -torch.ones_like(neg_h_index)
        pattern = torch.stack([neg_h_index, any], dim=-1)
        edge_index, num_t_truth = graph.match(pattern)
        t_truth_index = graph.edge_list[edge_index, 1]
        pos_index = torch.repeat_interleave(num_t_truth)
        t_mask = torch.ones(count, self.num_node, dtype=torch.bool, device=self.device)
        t_mask[pos_index, t_truth_index] = 0
        t_mask.scatter_(1, neg_h_index.unsqueeze(-1), 0)
        neg_t_candidate = t_mask.nonzero()[:, 1]
        num_t_candidate = t_mask.sum(dim=-1)
        neg_t_index = functional.variadic_sample(neg_t_candidate, num_t_candidate, 1).squeeze(-1)

        return neg_h_index, neg_t_index

    def predict_and_target(self, batch, all_loss=None, metric=None):
        batch_size = len(batch)
        pos_h_index, pos_t_index = batch.t()

        if self.split == "train":
            num_negative = self.num_negative
        else:
            num_negative = 1
        if self.strict_negative or self.split != "train":
            neg_h_index, neg_t_index = self._strict_negative(batch_size * num_negative, self.split)
        else:
            neg_h_index, neg_t_index = torch.randint(self.num_node, (2, batch_size * num_negative), device=self.device)
        neg_h_index = neg_h_index.view(batch_size, num_negative)
        neg_t_index = neg_t_index.view(batch_size, num_negative)
        h_index = pos_h_index.unsqueeze(-1).repeat(1, num_negative + 1)
        t_index = pos_t_index.unsqueeze(-1).repeat(1, num_negative + 1)
        h_index[:, 1:] = neg_h_index
        t_index[:, 1:] = neg_t_index

        pred = self.model(self.train_graph, h_index, t_index, all_loss=all_loss, metric=metric)
        target = torch.zeros_like(pred)
        target[:, 0] = 1
        return pred, target

    def evaluate(self, pred, target):
        pred = pred.flatten()
        target = target.flatten()

        metric = {}
        for _metric in self.metric:
            if _metric == "auroc":
                score = metrics.area_under_roc(pred, target)
            elif _metric == "ap":
                score = metrics.area_under_prc(pred, target)
            else:
                raise ValueError("Unknown metric `%s`" % _metric)

            name = tasks._get_metric_name(_metric)
            metric[name] = score

        return metric


@R.register("tasks.InductiveKnowledgeGraphCompletion")
class InductiveKnowledgeGraphCompletion(tasks.KnowledgeGraphCompletion, core.Configurable):

    def __init__(self, model, criterion="bce", metric=("mr", "mrr", "hits@1", "hits@3", "hits@10", "hits@10_50"),
                 num_negative=128, margin=6, adversarial_temperature=0, strict_negative=True, sample_weight=True):
        super(InductiveKnowledgeGraphCompletion, self).__init__(
            model, criterion, metric, num_negative, margin, adversarial_temperature, strict_negative,
            sample_weight=sample_weight)

    def preprocess(self, train_set, valid_set, test_set):
        if isinstance(train_set, torch_data.Subset):
            dataset = train_set.dataset
        else:
            dataset = train_set
        self.num_entity = dataset.num_entity
        self.num_relation = dataset.num_relation
        self.register_buffer("fact_graph", dataset.graph)

        if self.sample_weight:
            degree_hr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            degree_tr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            for h, t, r in train_set:
                degree_hr[h, r] += 1
                degree_tr[t, r] += 1
            self.register_buffer("degree_hr", degree_hr)
            self.register_buffer("degree_tr", degree_tr)

        self.register_buffer("train_graph", dataset.train_graph)
        self.register_buffer("valid_graph", dataset.valid_graph)
        self.register_buffer("test_graph", dataset.test_graph)

        return train_set, valid_set, test_set

    def predict(self, batch, all_loss=None, metric=None):
        pos_h_index, pos_t_index, pos_r_index = batch.t()
        batch_size = len(batch)
        graph = getattr(self, "%s_graph" % self.split)

        if all_loss is None:
            # test
            all_index = torch.arange(graph.num_node, device=self.device)
            t_preds = []
            h_preds = []
            for neg_index in all_index.split(self.num_negative):
                r_index = pos_r_index.unsqueeze(-1).expand(-1, len(neg_index))
                h_index, t_index = torch.meshgrid(pos_h_index, neg_index)
                t_pred = self.model(graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)
                t_preds.append(t_pred)
            t_pred = torch.cat(t_preds, dim=-1)
            for neg_index in all_index.split(self.num_negative):
                r_index = pos_r_index.unsqueeze(-1).expand(-1, len(neg_index))
                t_index, h_index = torch.meshgrid(pos_t_index, neg_index)
                h_pred = self.model(graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)
                h_preds.append(h_pred)
            h_pred = torch.cat(h_preds, dim=-1)
            pred = torch.stack([t_pred, h_pred], dim=1)
            # in case of GPU OOM
            pred = pred.cpu()
        else:
            # train
            if self.strict_negative:
                neg_index = self._strict_negative(pos_h_index, pos_t_index, pos_r_index)
            else:
                neg_index = torch.randint(self.num_entity, (batch_size, self.num_negative), device=self.device)
            h_index = pos_h_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            t_index = pos_t_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            r_index = pos_r_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            t_index[:batch_size // 2, 1:] = neg_index[:batch_size // 2]
            h_index[batch_size // 2:, 1:] = neg_index[batch_size // 2:]
            pred = self.model(graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)

        return pred

    def target(self, batch):
        # test target
        batch_size = len(batch)
        graph = getattr(self, "%s_graph" % self.split)
        pos_h_index, pos_t_index, pos_r_index = batch.t()
        any = -torch.ones_like(pos_h_index)

        pattern = torch.stack([pos_h_index, any, pos_r_index], dim=-1)
        edge_index, num_t_truth = graph.match(pattern)
        t_truth_index = graph.edge_list[edge_index, 1]
        pos_index = torch.repeat_interleave(num_t_truth)
        t_mask = torch.ones(batch_size, graph.num_node, dtype=torch.bool, device=self.device)
        t_mask[pos_index, t_truth_index] = 0

        pattern = torch.stack([any, pos_t_index, pos_r_index], dim=-1)
        edge_index, num_h_truth = graph.match(pattern)
        h_truth_index = graph.edge_list[edge_index, 0]
        pos_index = torch.repeat_interleave(num_h_truth)
        h_mask = torch.ones(batch_size, graph.num_node, dtype=torch.bool, device=self.device)
        h_mask[pos_index, h_truth_index] = 0

        mask = torch.stack([t_mask, h_mask], dim=1)
        target = torch.stack([pos_t_index, pos_h_index], dim=1)

        # in case of GPU OOM
        return mask.cpu(), target.cpu()

    def evaluate(self, pred, target):
        mask, target = target

        pos_pred = pred.gather(-1, target.unsqueeze(-1))
        ranking = torch.sum((pos_pred <= pred) & mask, dim=-1) + 1

        metric = {}
        for _metric in self.metric:
            if _metric == "mr":
                score = ranking.float().mean()
            elif _metric == "mrr":
                score = (1 / ranking.float()).mean()
            elif _metric.startswith("hits@"):
                values = _metric[5:].split("_")
                threshold = int(values[0])
                if len(values) > 1:
                    num_sample = int(values[1])
                    # unbiased estimation
                    fp_rate = (ranking - 1).float() / mask.sum(dim=-1)
                    score = 0
                    for i in range(threshold):
                        # choose i false positive from num_sample negatives
                        num_comb = math.factorial(num_sample) / math.factorial(i) / math.factorial(num_sample - i)
                        score += num_comb * (fp_rate ** i) * ((1 - fp_rate) ** (num_sample - i))
                    score = score.mean()
                else:
                    score = (ranking <= threshold).float().mean()
            else:
                raise ValueError("Unknown metric `%s`" % _metric)

            name = tasks._get_metric_name(_metric)
            metric[name] = score

        return metric


@R.register("tasks.KnowledgeGraphCompletionOGB")
class KnowledgeGraphCompletionOGB(tasks.KnowledgeGraphCompletion, core.Configurable):

    def __init__(self, model, criterion="bce", evaluator=None, num_negative=128, margin=6, adversarial_temperature=0,
                 strict_negative=True, heterogeneous_negative=False, fact_ratio=None, sample_weight=True):
        super(KnowledgeGraphCompletionOGB, self).__init__(
            model, criterion, None, num_negative, margin, adversarial_temperature, strict_negative, True,
            fact_ratio, sample_weight)

        self.evaluator = evaluator
        self.heterogeneous_negative = heterogeneous_negative

    def preprocess(self, train_set, valid_set, test_set):
        if isinstance(train_set, torch_data.Subset):
            dataset = train_set.dataset
        else:
            dataset = train_set
        self.num_entity = dataset.num_entity
        self.num_relation = dataset.num_relation
        self.register_buffer("graph", dataset.graph)
        fact_mask = torch.zeros(len(dataset), dtype=torch.bool)
        fact_mask[train_set.indices] = 1
        if self.fact_ratio:
            length = int(len(train_set) * self.fact_ratio)
            index = torch.randperm(len(train_set))[length:]
            train_indices = torch.tensor(train_set.indices)
            fact_mask[train_indices[index]] = 0
            train_set = torch_data.Subset(train_set, index)
        self.register_buffer("fact_graph", dataset.graph.edge_mask(fact_mask))

        if self.sample_weight:
            degree_hr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            degree_tr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            for h, t, r in train_set:
                degree_hr[h, r] += 1
                degree_tr[t, r] += 1
            self.register_buffer("degree_hr", degree_hr)
            self.register_buffer("degree_tr", degree_tr)

        return train_set, valid_set, test_set

    @torch.no_grad()
    def _strict_negative(self, pos_h_index, pos_t_index, pos_r_index):
        batch_size = len(pos_h_index)
        any = -torch.ones_like(pos_h_index)
        node_type = self.fact_graph.node_type

        pattern = torch.stack([pos_h_index, any, pos_r_index], dim=-1)
        pattern = pattern[:batch_size // 2]
        edge_index, num_t_truth = self.fact_graph.match(pattern)
        t_truth_index = self.fact_graph.edge_list[edge_index, 1]
        pos_index = torch.repeat_interleave(num_t_truth)
        if self.heterogeneous_negative:
            pos_t_type = node_type[pos_t_index[:batch_size // 2]]
            t_mask = pos_t_type.unsqueeze(-1) == node_type.unsqueeze(0)
        else:
            t_mask = torch.ones(len(pattern), self.num_entity, dtype=torch.bool, device=self.device)
        t_mask[pos_index, t_truth_index] = 0
        neg_t_candidate = t_mask.nonzero()[:, 1]
        num_t_candidate = t_mask.sum(dim=-1)
        neg_t_index = functional.variadic_sample(neg_t_candidate, num_t_candidate, self.num_negative)

        pattern = torch.stack([any, pos_t_index, pos_r_index], dim=-1)
        pattern = pattern[batch_size // 2:]
        edge_index, num_h_truth = self.fact_graph.match(pattern)
        h_truth_index = self.fact_graph.edge_list[edge_index, 0]
        pos_index = torch.repeat_interleave(num_h_truth)
        if self.heterogeneous_negative:
            pos_h_type = node_type[pos_h_index[batch_size // 2:]]
            h_mask = pos_h_type.unsqueeze(-1) == node_type.unsqueeze(0)
        else:
            h_mask = torch.ones(len(pattern), self.num_entity, dtype=torch.bool, device=self.device)
        h_mask[pos_index, h_truth_index] = 0
        neg_h_candidate = h_mask.nonzero()[:, 1]
        num_h_candidate = h_mask.sum(dim=-1)
        neg_h_index = functional.variadic_sample(neg_h_candidate, num_h_candidate, self.num_negative)

        neg_index = torch.cat([neg_t_index, neg_h_index])

        return neg_index

    def predict(self, batch, all_loss=None, metric=None):
        batch_size = len(batch)

        if all_loss is None:
            # test
            h_index, t_index, r_index = batch.unbind(-1)
            pattern = batch[:, 0, :]
            num_match = self.fact_graph.match(pattern)[1]
            assert (num_match == 0).all()
            pred = self.model(self.fact_graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)
            # in case of GPU OOM
            pred = pred.cpu()
        else:
            # train
            pos_h_index, pos_t_index, pos_r_index = batch.t()
            if self.strict_negative:
                neg_index = self._strict_negative(pos_h_index, pos_t_index, pos_r_index)
            else:
                neg_index = torch.randint(self.num_entity, (batch_size, self.num_negative), device=self.device)
            h_index = pos_h_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            t_index = pos_t_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            r_index = pos_r_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
            t_index[:batch_size // 2, 1:] = neg_index[:batch_size // 2]
            h_index[batch_size // 2:, 1:] = neg_index[batch_size // 2:]
            pred = self.model(self.fact_graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)

        return pred

    def target(self, batch):
        # test target
        batch_size = len(batch)
        target = torch.zeros(batch_size, dtype=torch.long, device=self.device)
        # in case of GPU OOM
        return target.cpu()

    def evaluate(self, pred, target):
        is_positive = torch.zeros(pred.shape, dtype=torch.bool)
        is_positive.scatter_(-1, target.unsqueeze(-1), 1)
        pos_pred = pred[is_positive]
        neg_pred = pred[~is_positive].view(len(pos_pred), -1)
        metric = self.evaluator.eval({"y_pred_pos": pos_pred, "y_pred_neg": neg_pred})

        new_metric = {}
        for key in metric:
            new_key = key.split("_")[0]
            new_metric[new_key] = metric[key].mean()

        return new_metric
    
@R.register("tasks.KnowledgeGraphCompletionBiomed")
class KnowledgeGraphCompletionBiomed(tasks.KnowledgeGraphCompletion, core.Configurable):

    def __init__(self, model, criterion="bce",
                 metric=("mr", "mrr", "hits@1", "hits@3", "hits@10"),
                 num_negative=128, margin=6, adversarial_temperature=0, strict_negative=True,
                 heterogeneous_negative=False, heterogeneous_evaluation=False, filtered_ranking=True,
                 fact_ratio=None, sample_weight=True, gene_annotation_predict=False, conditional_probability=False):
        super(KnowledgeGraphCompletionBiomed, self).__init__(model=model, criterion=criterion, metric=metric, 
                                                             num_negative=num_negative, margin=margin,
                                                             adversarial_temperature=adversarial_temperature, 
                                                             strict_negative=strict_negative,
                                                             filtered_ranking=filtered_ranking,fact_ratio=fact_ratio,
                                                             sample_weight=sample_weight)
        self.heterogeneous_negative = heterogeneous_negative
        self.heterogeneous_evaluation = heterogeneous_evaluation
        self.gene_annotation_predict = gene_annotation_predict
        self.conditional_probability = conditional_probability
        
    def preprocess(self, train_set, valid_set, test_set):
        if isinstance(train_set, torch_data.Subset):
            dataset = train_set.dataset
        else:
            dataset = train_set
        self.num_entity = dataset.num_entity
        self.num_relation = dataset.num_relation
        self.register_buffer("graph", dataset.graph)
        fact_mask = torch.ones(len(dataset), dtype=torch.bool)
        fact_mask[valid_set.indices] = 0
        fact_mask[test_set.indices] = 0
        if self.fact_ratio:
            length = int(len(train_set) * self.fact_ratio)
            index = torch.randperm(len(train_set))[length:]
            train_indices = torch.tensor(train_set.indices)
            fact_mask[train_indices[index]] = 0
            train_set = torch_data.Subset(train_set, index)
        self.register_buffer("fact_graph", dataset.graph.edge_mask(fact_mask))

        if self.sample_weight:
            degree_hr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            degree_tr = torch.zeros(self.num_entity, self.num_relation, dtype=torch.long)
            for h, t, r in train_set:
                degree_hr[h, r] += 1
                degree_tr[t, r] += 1
            self.register_buffer("degree_hr", degree_hr)
            self.register_buffer("degree_tr", degree_tr)


        return train_set, valid_set, test_set     
        


    def target(self, batch):
        # test target
        batch_size = len(batch)
        pos_h_index, pos_t_index, pos_r_index = batch.t()
        any = -torch.ones_like(pos_h_index)
        node_type = self.fact_graph.node_type

        pattern = torch.stack([pos_h_index, any, pos_r_index], dim=-1)
        edge_index, num_t_truth = self.graph.match(pattern)
        t_truth_index = self.graph.edge_list[edge_index, 1]
        pos_index = torch.repeat_interleave(num_t_truth)
        t_mask = torch.ones(batch_size, self.num_entity, dtype=torch.bool, device=self.device)
        t_mask[pos_index, t_truth_index] = 0
        if self.heterogeneous_evaluation:
            t_mask[node_type.unsqueeze(0) != node_type[pos_t_index].unsqueeze(-1)] = 0

        pattern = torch.stack([any, pos_t_index, pos_r_index], dim=-1)
        edge_index, num_h_truth = self.graph.match(pattern)
        h_truth_index = self.graph.edge_list[edge_index, 0]
        pos_index = torch.repeat_interleave(num_h_truth)
        h_mask = torch.ones(batch_size, self.num_entity, dtype=torch.bool, device=self.device)
        h_mask[pos_index, h_truth_index] = 0
        if self.heterogeneous_evaluation:
            h_mask[node_type.unsqueeze(0) != node_type[pos_h_index].unsqueeze(-1)] = 0

        mask = torch.stack([t_mask, h_mask], dim=1)
        target = torch.stack([pos_t_index, pos_h_index], dim=1)

        # in case of GPU OOM
        return mask.cpu(), target.cpu()

    def evaluate(self, pred, target):
        mask, target = target

        pos_pred = pred.gather(-1, target.unsqueeze(-1))
        if self.filtered_ranking:
            ranking = torch.sum((pos_pred <= pred) & mask, dim=-1) + 1
        else:
            ranking = torch.sum(pos_pred <= pred, dim=-1) + 1
        
        pred = pred.flatten()
        target = target.flatten()
        metric = {}
        for _metric in self.metric:
            if _metric == "mr":
                score = ranking.float().mean()
            elif _metric == "mrr":
                score = (1 / ranking.float()).mean()
            elif _metric.startswith("hits@"):
                threshold = int(_metric[5:])
                score = (ranking <= threshold).float().mean()
            elif _metric == "auroc":
                score = metrics.area_under_roc(pred, target)
            elif _metric == "ap":
                score = metrics.area_under_prc(pred, target)
            else:
                raise ValueError("Unknown metric `%s`" % _metric)
            name = tasks._get_metric_name(_metric)
            metric[name] = score

        return metric
    
    def predict(self, batch, dataset=dataset, all_loss=None, metric=None):
        pos_h_index, pos_t_index, pos_r_index = batch.t()
        batch_size = len(batch)

        if all_loss is None:
            # test
            if self.gene_annotation_predict:
                # change all_index to only evaluation against GO terms
                nodes = dataset.entity_vocab
                nodes__dict={ix: val for ix, val in enumerate(nodes)}
                go_id = [key for key, val in nodes__dict.items() if val.startswith('GO:')]
                all_index =torch.tensor(go_id,  device=self.device) # evaluate against only GO terms
            else:
                all_index = torch.arange(self.num_entity, device=self.device) # evaluate against all nodes

            t_preds = []
            h_preds = []
            num_negative = self.num_entity if self.full_batch_eval else self.num_negative
            for neg_index in all_index.split(num_negative):
                r_index = pos_r_index.unsqueeze(-1).expand(-1, len(neg_index))
                h_index, t_index = torch.meshgrid(pos_h_index, neg_index)
                t_pred = self.model(self.fact_graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)
                t_preds.append(t_pred)
            t_pred = torch.cat(t_preds, dim=-1)
            for neg_index in all_index.split(num_negative):
                r_index = pos_r_index.unsqueeze(-1).expand(-1, len(neg_index))
                t_index, h_index = torch.meshgrid(pos_t_index, neg_index)
                h_pred = self.model(self.fact_graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric)
                h_preds.append(h_pred)
                
            h_pred = torch.cat(h_preds, dim=-1)
            pred = torch.stack([t_pred, h_pred], dim=1)
            # in case of GPU OOM
            pred = pred.cpu()
        else:
            # train
            if self.conditional_probability:
                if self.strict_negative:
                    neg_index = self._strict_negative(pos_h_index, pos_t_index, pos_r_index)
                else:
                    neg_index = torch.randint(self.num_entity, (batch_size, self.num_negative), device=self.device)
                h_index = pos_h_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                t_index = pos_t_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                r_index = pos_r_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                t_index[:batch_size // 2, 1:] = neg_index[:batch_size // 2]
                h_index[batch_size // 2:, 1:] = neg_index[batch_size // 2:]
                #import pdb; pdb.set_trace()

                
            else:
                # add graph to undirected to calculate degree_in_type
                # parse the modified graph to forward
                graph = self.fact_graph
                graph = graph.undirected(add_inverse=True)
                
                # calculate degree_in_type to use in strict_negative
                ########################
                # add the degree_in_type
                ########################

                # get the graph, nodes, edge_list
                node_type = graph.node_type
                                
                # get node type of tail
                node_type_t = node_type[graph.edge_list[:, 1]]
                
                # count the number of occurance for each node to type t
                myindex = graph.edge_list[:, 0]
                myinput = torch.t(F.one_hot(node_type_t))  # one hot encoding of node types
                degree_in_type = myinput.new_zeros(len(node_type.unique()),  graph.num_node)
                
                degree_in_type = scatter_add(myinput, myindex, out=degree_in_type)
                num_nodes_per_type = torch.bincount(node_type)
                
                ########################
                ########################
                if self.strict_negative:
                    neg_h_index, neg_t_index = self._strict_negative(pos_h_index, pos_t_index, pos_r_index, degree_in_type, num_nodes_per_type, graph)
                    #TODO: does it make sense to register another graph for the undirected case?
                else:
                    neg_h_index, neg_t_index = torch.randint(self.num_node, (2, batch_size * self.num_negative), device=self.device)
                # make dim 0 batch size and dim 1 negative samples
                neg_h_index = neg_h_index.view(batch_size, self.num_negative)
                neg_t_index = neg_t_index.view(batch_size, self.num_negative)
                # repeat one more time than the number of negative samples [32,33]
                h_index = pos_h_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                t_index = pos_t_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                r_index = pos_r_index.unsqueeze(-1).repeat(1, self.num_negative + 1)
                # first one is true head and tail, rest are the negative samples for head and tail
                h_index[:, 1:] = neg_h_index
                t_index[:, 1:] = neg_t_index
                

            #import pdb; pdb.set_trace()
            pred = self.model(graph, h_index, t_index, r_index, all_loss=all_loss, metric=metric, conditional_probability = self.conditional_probability)
            
        return pred
    

    
    @torch.no_grad()
    def _strict_negative(self, pos_h_index, pos_t_index, pos_r_index, degree_in_type, num_nodes_per_type, graph):
        # TODO: set default for degree_in_type and num_nodes_per_type, in case of not using them
        if self.conditional_probability:
            # conditional probaility - classical KG setting
            batch_size = len(pos_h_index)
            any = -torch.ones_like(pos_h_index)
            node_type = self.fact_graph.node_type

            pattern = torch.stack([pos_h_index, any, pos_r_index], dim=-1)
            pattern = pattern[:batch_size // 2]
            edge_index, num_t_truth = self.fact_graph.match(pattern)
            t_truth_index = self.fact_graph.edge_list[edge_index, 1]
            pos_index = torch.repeat_interleave(num_t_truth)
            if self.heterogeneous_negative:
                pos_t_type = node_type[pos_t_index[:batch_size // 2]]
                t_mask = pos_t_type.unsqueeze(-1) == node_type.unsqueeze(0)
            else:
                t_mask = torch.ones(len(pattern), self.num_entity, dtype=torch.bool, device=self.device)
            t_mask[pos_index, t_truth_index] = 0
            neg_t_candidate = t_mask.nonzero()[:, 1]
            num_t_candidate = t_mask.sum(dim=-1)
            neg_t_index = functional.variadic_sample(neg_t_candidate, num_t_candidate, self.num_negative)

            pattern = torch.stack([any, pos_t_index, pos_r_index], dim=-1)
            pattern = pattern[batch_size // 2:]
            edge_index, num_h_truth = self.fact_graph.match(pattern)
            h_truth_index = self.fact_graph.edge_list[edge_index, 0]
            pos_index = torch.repeat_interleave(num_h_truth)
            if self.heterogeneous_negative:
                pos_h_type = node_type[pos_h_index[batch_size // 2:]]
                h_mask = pos_h_type.unsqueeze(-1) == node_type.unsqueeze(0)
            else:
                h_mask = torch.ones(len(pattern), self.num_entity, dtype=torch.bool, device=self.device)
            h_mask[pos_index, h_truth_index] = 0
            neg_h_candidate = h_mask.nonzero()[:, 1]
            num_h_candidate = h_mask.sum(dim=-1)
            neg_h_index = functional.variadic_sample(neg_h_candidate, num_h_candidate, self.num_negative)

            neg_index = torch.cat([neg_t_index, neg_h_index])
            return neg_index
        
        else:
            # joint probaility - better for finding all missing links
            node_type = graph.node_type
            
            ####################### 
            # sample from p(h)
            #######################
            
            # find the node types of pos_t
            pos_t_type = node_type[pos_t_index]
            pos_h_type = node_type[pos_h_index]
            
            # index the  degree of node h connecting to type t
            # number of nodes of type(t) - degree of node h connecting to type t
            
            prob = (num_nodes_per_type[pos_t_type].unsqueeze(1) - degree_in_type[pos_t_type]).float()
            #TODO: also yields negative values, bcs of the num_nodes_per_type - degree_in_type (from undirected graph)
            
            # if type_h == type_t, remove one from prob
            same_type_mask = pos_t_type == pos_h_type
            prob[same_type_mask] -= 1
            # set to 0, if not from desired node type
            h_mask = node_type.unsqueeze(0) != pos_h_type.unsqueeze(1)
            prob[h_mask] = 0     
            
            # sample from the distribution
            neg_h_index = functional.multinomial(prob, self.num_negative, replacement=True)
            neg_h_index = torch.flatten(neg_h_index)

            ####################### 
            # sample from p(t|h)
            #######################
            any = -torch.ones_like(neg_h_index)
            
            # find all the edges from neg_h_index that EXISTS
            pattern = torch.stack([neg_h_index, any, pos_r_index.repeat_interleave(self.num_negative)], dim=-1)
            edge_index, num_t_truth = graph.match(pattern)
            t_truth_index = graph.edge_list[edge_index, 1]
        
            pos_index = torch.repeat_interleave(num_t_truth)
            
            # heterogeneous
            if self.heterogeneous_negative:
                pos_t_type = node_type[pos_t_index].repeat_interleave(self.num_negative)
                t_mask = pos_t_type.unsqueeze(-1) == node_type.unsqueeze(0)
            else:
                t_mask = torch.ones(len(pattern), self.num_entity, dtype=torch.bool, device=self.device)
            
            # exclude those that exists
            t_mask[pos_index, t_truth_index] = 0
            t_mask.scatter_(1, neg_h_index.unsqueeze(-1), 0)
            neg_t_candidate = t_mask.nonzero()[:, 1]
            num_t_candidate = t_mask.sum(dim=-1)
            neg_t_index = functional.variadic_sample(neg_t_candidate, num_t_candidate, 1).squeeze(-1)
            
            return neg_h_index, neg_t_index
    
    

import pandas as pd

df = pd.read_csv('predictions.csv', sep='\t', header=0)
df = df[(df.query_node == "MALAT1") & (df.probability > 0.5) & (df.reverse == 0)]
df["query_relation"] = df["query_relation"] .apply(lambda x: x[:-4])
df = df[["query_node", "query_relation", "prediction_node", "probability"]].drop_duplicates()


# df["sum_probability"] = df.groupby(["query_node", 'prediction_node'])["probability"].transform('sum')
# df = df[["query_node", "prediction_node", "sum_probability"]].drop_duplicates()
# df.to_csv("malat_sum.csv", sep='\t', header=True, index=False)

lnctard = pd.read_csv('lnctard.txt', sep='\t', header=0)


df = df.merge(lnctard, how="left", left_on=["query_node", "query_relation", "prediction_node"], right_on=["Regulator", "SearchregulatoryMechanism", "Target"])
df["is_novel"] = (df["Target"].isna())
df = df[["query_node", "query_relation", "prediction_node", "probability", "is_novel"]]

# df = df[df.query_relation == "transcriptional regulation"]
df.to_csv("malat_predictions.csv", sep='\t', header=True, index=False)
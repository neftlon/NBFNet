#!/bin/bash
#SBATCH --job-name=predict
#SBATCH --output=./slurm_out/job_output_pred.txt
#SBATCH --error=./slurm_out/job_error_pred.txt
#SBATCH --time=02:00:00
#SBATCH --mem=24Gb
#SBATCH -c 4
#SBATCH --gres=gpu:1
#SBATCH --partition=main


module load python
source ~/allvirtualenvs/nbfnet/bin/activate # activate the virtualenv
module load cuda/11.1
cd /home/mila/y/yue.hu/github/NBFNet

#python -m torch.distributed.launch --nproc_per_node=4 script/run.py -c config/knowledge_graph/biogrid.yaml --gpus [0,1]

python script/predict.py -c config/knowledge_graph/qki_factgraph.yaml --gpus [0] --version v1 --checkpoint /home/mila/y/yue.hu/experiments/KnowledgeGraphComponBiomed/biomedical/NBFNet/2022-11-18-12-59-19/model_epoch_5.pth

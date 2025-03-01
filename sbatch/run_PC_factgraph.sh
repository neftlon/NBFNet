#!/bin/bash
#SBATCH --job-name=biomed
#SBATCH --output=./slurm_out/job_output.txt
#SBATCH --error=./slurm_out/job_error.txt
#SBATCH --time=12:00:00 # takes around 7 hours to run
#SBATCH --mem=24Gb
#SBATCH -c 4
#SBATCH --gres=gpu:1
#SBATCH --partition=main


module load python
source ~/allvirtualenvs/nbfnet/bin/activate # activate the virtualenv
module load cuda/11.1
cd /home/mila/y/yue.hu/github/NBFNet

#python -m torch.distributed.launch --nproc_per_node=4 script/run.py -c config/knowledge_graph/biogrid.yaml --gpus [0,1]

python script/run.py -c config/knowledge_graph/submission_PC_factgraph.yaml --gpus [0] --version v1

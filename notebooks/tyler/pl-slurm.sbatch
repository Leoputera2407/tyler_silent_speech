#!/usr/bin/bash
#SBATCH --output=%j.out
#SBATCH --error=%j.err
#SBATCH --time=0:10:00
#SBATCH -p deissero,gpu,owners
#SBATCH --nodes=1
#SBATCH -c 6
#SBATCH --gpus-per-node=2
#SBATCH --ntasks-per-node=2
#SBATCH --mem-per-cpu=4000
#SBATCH --constraint="GPU_MEM:32GB|GPU_MEM:40GB|GPU_MEM:48GB|GPU_MEM:80GB"

# --gres:gpu=N, --ntasks-per-node N, and --devices N must all be the same value
# https://github.com/Lightning-AI/lightning/issues/16828
# TODO: auto wal/ql-time resubmittion:
# https://lightning.ai/docs/pytorch/stable/clouds/cluster_advanced.html#enable-auto-wall-time-resubmitions
. activate magneto
ml nccl/2.17.1
cd ~/code/silent_speech/notebooks/tyler/
# srun python 2023-08-16_brain_to_text.py --learning-rate="6e-4" --devices=2
srun python 2023-08-24_pl_slurm_bullshit2.py

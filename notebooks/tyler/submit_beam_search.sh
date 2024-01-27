#!/bin/bash
#SBATCH -p henderj,deissero,shauld
#SBATCH --time=48:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=60G

. activate magneto
cd /home/users/tbenst/code/silent_speech/notebooks/tyler
srun python 2024-01-26_icml_beams.py "$@"
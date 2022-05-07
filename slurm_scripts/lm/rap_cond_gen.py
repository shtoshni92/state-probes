from itertools import product
from os import path
import os
import subprocess

JOB_NAME = f"rap_cond_gen"

out_dir = path.join(os.getcwd(), f"slurm_scripts/outputs/{JOB_NAME}")
if not path.isdir(out_dir):
    os.makedirs(out_dir)

# Write commands to output file
out_file = path.join(out_dir, "commands.txt")
base_dir = "/share/data/speech/shtoshni/research/state-probes"

fixed = [
    "--epochs 100 --patience 10 --use_wandb --model_size base --seed 25",
]
# '--epochs 100 --patience 10 --use_wandb --model_size large --seed 45']
state = ["--add_state " + state_type for state_type in ["all"]] + [""]
# rap_prob = [f"--rap_prob {rap_prob}" for rap_prob in [0.1, 0.25, 0.5, 0.75]]
# model_size = [f'--model_size {model_size}' for model_size in ['base', 'large']]
common_options = [fixed, state]


with open(out_file, "w") as out_f:
    for option_comb in product(*common_options):
        # print(option_comb)
        base = "{}/slurm_scripts/lm/run.sh ".format(base_dir)
        cur_command = base
        for value in option_comb:
            cur_command += value + " "

        cur_command = cur_command.strip()
        out_f.write(cur_command + "\n")

subprocess.call(
    "cd {}; python ~/slurm_batch.py {} -J {} --constraint 2080ti".format(
        out_dir, out_file, JOB_NAME
    ),
    shell=True,
)
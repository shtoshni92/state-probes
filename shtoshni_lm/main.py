import os
import argparse
import wandb
from os import path

from shtoshni_lm.experiment import Experiment


def main(command_options=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--batchsize", type=int, default=24)
    parser.add_argument("--eval", default=False, action="store_true")
    parser.add_argument("--seed", type=int, default=10)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--num_train", type=int, default=None)
    parser.add_argument("--num_dev", type=int, default=None)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--base_model_dir", type=str, default="models")
    parser.add_argument("--model_size", type=str, default="base", choices=["base", "large"])
    parser.add_argument("--use_wandb", default=False, action="store_true")
    parser.add_argument("--rap_prob", type=float, default=0.0, help="Probability of using state")

    parser.add_argument(
        "--add_state",
        default="",
        choices=[
            "ras",  # Randomly added state
            "explanation",  # State is always added; Treated as an explanation
            "multitask",  # Multitask with state prediction
            "",
        ],
        type=str,
        help="Mechanism for adding state (via random addition/multitasking/explanation i.e. always)",
    )

    parser.add_argument(
        "--state_repr",
        choices=["all"],  # "targeted", "random", None],
        type=str,
        default="all",
        help="Representation of state string",
    )

    if command_options is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(command_options)

    model_dir_str = ""
    model_dir_str += "size_" + str(args.model_size)
    model_dir_str += "_epochs_" + str(args.epochs)
    model_dir_str += "_patience_" + str(args.patience)

    if args.add_state:
        model_dir_str += "_state"
        model_dir_str += f"_{args.add_state}"
        model_dir_str += f"_{args.state_repr}"

        if args.add_state == "ras" or args.add_state == "multitask":
            assert args.rap_prob > 0.0
            model_dir_str += f"_rap_{args.rap_prob}"

    if args.num_train is not None:
        model_dir_str += f"_num_train_{args.num_train}"

    model_dir_str += f"_seed_{args.seed}"

    args.model_dir = path.join(args.base_model_dir, model_dir_str)
    args.best_model_dir = path.join(args.model_dir, "best")

    # Set log dir
    wandb_dir = path.join(args.model_dir, "wandb")
    os.environ["WANDB_DIR"] = wandb_dir

    if not path.exists(args.model_dir):
        os.makedirs(args.model_dir)

    if not path.exists(args.best_model_dir):
        os.makedirs(args.best_model_dir)

    if not path.exists(wandb_dir):
        os.makedirs(wandb_dir)

    args.model_path = path.join(args.model_dir, "model.pt")
    args.best_model_path = path.join(args.best_model_dir, "model.pt")

    if args.use_wandb:
        try:
            wandb.init(
                id=model_dir_str,
                project="state-probing",
                resume=True,
                notes="State probing",
                tags="november",
                config={},
            )
            wandb.config.update(args)
        except:
            args.use_wandb = False
    Experiment(args)


if __name__ == "__main__":
    main()

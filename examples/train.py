import torch
from torch.utils.tensorboard import SummaryWriter
from boltzmann import protein
from boltzmann.generative import transforms
from boltzmann import nn
from boltzmann import utils
from simtk import openmm as mm
from simtk.openmm import app
import numpy as np
import mdtraj as md
import os
import shutil
import argparse
from tqdm import tqdm


def get_device():
    if torch.cuda.is_available():
        print("Using cuda")
        device = torch.device("cuda")
    else:
        print("Using CPU")
        device = torch.device("cpu")
    return device


def delete_run(name):
    if os.path.exists(f"models/{name}.pkl"):
        os.remove(f"models/{name}.pkl")
    if os.path.exists(f"training_traj/{name}.pdb"):
        os.remove(f"training_traj/{name}.pdb")
    if os.path.exists(f"sample_traj/{name}.pdb"):
        os.remove(f"sample_traj/{name}.pdb")
    if os.path.exists(f"runs/{name}"):
        shutil.rmtree(f"runs/{name}")


def create_dirs():
    os.makedirs("models", exist_ok=True)
    os.makedirs("training_traj", exist_ok=True)
    os.makedirs("sample_traj", exist_ok=True)


def create_tensorboard(name):
    return SummaryWriter(log_dir=f"runs/{name}", purge_step=0)


def load_trajectory(pdb_path, dcd_path):
    print("Loading trajectory")
    t = md.load(args.dcd_path, top=args.pdb_path)
    ind = t.topology.select("backbone")
    t.superpose(t, frame=0, atom_indices=ind)
    return t


def build_network(
    n_dim,
    topology,
    training_data,
    n_coupling,
    use_affine_coupling,
    spline_points,
    hidden_features,
    hidden_layers,
    dropout_fraction,
    device,
):
    print("Creating network")
    layers = []

    # Create the mixed transofrm layer
    pca_block = protein.PCABlock("backbone", True)
    mixed = protein.MixedTransform(n_dim, topology, [pca_block], training_data)
    layers.append(mixed)

    # Create the coupling layers
    for _ in range(n_coupling):
        p = transforms.RandomPermutation(n_dim - 6, 1)
        mask_even = utils.create_alternating_binary_mask(features=n_dim - 6, even=True)
        mask_odd = utils.create_alternating_binary_mask(features=n_dim - 6, even=False)
        if use_affine_coupling:
            t1 = transforms.AffineCouplingTransform(
                mask=mask_even,
                transform_net_create_fn=lambda in_features, out_features: nn.ResidualNet(
                    in_features=in_features,
                    out_features=out_features,
                    hidden_features=hidden_features,
                    num_blocks=hidden_layers,
                    dropout_probability=dropout_fraction,
                    use_batch_norm=True,
                ),
            )
            t2 = transforms.AffineCouplingTransform(
                mask=mask_odd,
                transform_net_create_fn=lambda in_features, out_features: nn.ResidualNet(
                    in_features=in_features,
                    out_features=out_features,
                    hidden_features=hidden_features,
                    num_blocks=hidden_layers,
                    dropout_probability=dropout_fraction,
                    use_batch_norm=True,
                ),
            )
        else:
            t1 = transforms.PiecewiseRationalQuadraticCouplingTransform(
                mask=mask_even,
                transform_net_create_fn=lambda in_features, out_features: nn.ResidualNet(
                    in_features=in_features,
                    out_features=out_features,
                    hidden_features=hidden_features,
                    num_blocks=hidden_layers,
                    dropout_probability=dropout_fraction,
                    use_batch_norm=True,
                ),
                tails="linear",
                tail_bound=5,
                num_bins=spline_points,
                apply_unconditional_transform=False,
            )
            t2 = transforms.PiecewiseRationalQuadraticCouplingTransform(
                mask=mask_odd,
                transform_net_create_fn=lambda in_features, out_features: nn.ResidualNet(
                    in_features=in_features,
                    out_features=out_features,
                    hidden_features=hidden_features,
                    num_blocks=hidden_layers,
                    dropout_probability=dropout_fraction,
                    use_batch_norm=True,
                ),
                tails="linear",
                tail_bound=5,
                num_bins=spline_points,
                apply_unconditional_transform=False,
            )
        layers.append(p)
        layers.append(t1)
        layers.append(t2)

    net = transforms.CompositeTransform(layers).to(device)
    print(net)
    print_number_trainable_params(net)
    return net


def load_network(path, device):
    net = torch.load(path).to(device)
    print(net)
    print_number_trainable_params(net)
    return net


def setup_optimizer(net, init_lr, weight_decay):
    optimizer = torch.optim.AdamW(
        net.parameters(), lr=init_lr, weight_decay=weight_decay
    )
    return optimizer


def setup_scheduler(optimizer, init_lr, final_lr, epochs, warmup_epochs):
    anneal = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, epochs, final_lr)
    warmup = utils.GradualWarmupScheduler(
        optimizer, 8, warmup_epochs, after_scheduler=anneal
    )
    return warmup


def print_number_trainable_params(net):
    total_params = sum(p.numel() for p in net.parameters() if p.requires_grad)
    print()
    print(f"Network has {total_params} trainable parameters")
    print()


def get_openmm_context(pdb_path):
    pdb = app.PDBFile(pdb_path)
    ff = app.ForceField("amber99sbildn.xml", "amber99_obc.xml")
    system = ff.createSystem(
        pdb.topology,
        nonbondedMethod=app.CutoffNonPeriodic,
        nonbondedCutoff=1.0,
        constraints=None,
    )
    integrator = mm.LangevinIntegrator(298, 1.0, 0.002)
    simulation = app.Simulation(pdb.topology, system, integrator)
    context = simulation.context
    return context


def get_energy_evaluator(openmm_context, temperature, energy_high, energy_max, device):
    energy_high = torch.tensor(
        energy_high, dtype=torch.float32, device=device, requires_grad=False
    )
    energy_max = torch.tensor(
        energy_max, dtype=torch.float32, device=device, requires_grad=False
    )

    def eval_energy(x):
        return protein.regularize_energy(
            protein.openmm_energy(x, openmm_context, temperature),
            energy_high,
            energy_max,
        )

    return eval_energy


def run_training(args, device):
    writer = create_tensorboard(args.output_name)

    traj = load_trajectory(args.pdb_path, args.dcd_path)
    n_dim = traj.xyz.shape[1] * 3
    training_data_npy = traj.xyz.reshape(-1, n_dim)
    training_data = torch.from_numpy(training_data_npy.astype("float32"))
    print("Trajectory loaded")
    print("Data has size:", training_data.shape)

    if args.load_network:
        net = load_network(f"models/{args.load_network}.pkl", device=device)
    else:
        net = build_network(
            n_dim=n_dim,
            topology=traj.topology,
            training_data=training_data,
            n_coupling=args.coupling_layers,
            use_affine_coupling=args.is_affine,
            spline_points=args.spline_points,
            hidden_features=args.hidden_features,
            hidden_layers=args.hidden_layers,
            dropout_fraction=args.dropout_fraction,
            device=device,
        )

    optimizer = setup_optimizer(
        net=net, init_lr=args.init_lr, weight_decay=args.weight_decay
    )
    scheduler = setup_scheduler(
        optimizer,
        init_lr=args.init_lr,
        final_lr=args.final_lr,
        epochs=args.epochs,
        warmup_epochs=args.warmup_epochs,
    )

    openmm_context = get_openmm_context(args.pdb_path)
    energy_evaluator = get_energy_evaluator(
        openmm_context=openmm_context,
        temperature=args.temperature,
        energy_high=args.energy_high,
        energy_max=args.energy_max,
        device=device,
    )

    # Shuffle the training data
    n = training_data_npy.shape[0]
    n_val = int(n / args.fold_validation)
    np.random.shuffle(training_data_npy)

    # Split the training and validation sets
    val_data = torch.as_tensor(training_data_npy[:n_val, :], device=device)
    train_data = torch.as_tensor(training_data_npy[n_val:, :], device=device)
    indices = np.arange(train_data.shape[0])
    indices_val = np.arange(val_data.shape[0])

    # We're going choose a random latent vector and see what it transforms to
    # as we train the network.
    fixed_coords = []
    fixed_z = torch.normal(0, 1, size=(1, n_dim - 6), device=device)

    with tqdm(range(args.epochs)) as progress:
        for epoch in progress:
            net.train()
            optimizer.zero_grad()

            if args.train_example:
                index_batch = np.random.choice(indices, args.batch_size, replace=True)
                x_batch = train_data[index_batch, :]
                z, z_jac = net.forward(x_batch)
                example_loss = 0.5 * torch.mean(torch.sum(z ** 2, dim=1)) - torch.mean(
                    z_jac
                )
                example_loss = args.example_weight * example_loss

            if args.train_energy:
                z_batch = torch.normal(
                    0, 1, size=(args.batch_size, n_dim - 6), device=device
                )
                x, x_jac = net.inverse(z_batch)
                energies = energy_evaluator(x)
                energy_loss = torch.mean(energies) - torch.mean(x_jac)
                energy_loss = args.energy_weight * energy_loss

            if args.train_example and args.train_energy:
                loss = example_loss + energy_loss
            elif args.train_example:
                loss = example_loss
            else:
                loss = energy_loss

            loss.backward()
            optimizer.step()
            scheduler.step(epoch)

            if epoch % args.log_freq == 0:
                net.eval()

                # Output our training losses
                writer.add_scalar("Train/loss", loss.item(), epoch)
                if args.train_example:
                    writer.add_scalar("Train/example", example_loss.item(), epoch)
                if args.train_energy:
                    writer.add_scalar("Train/energy", energy_loss.item(), epoch)

                # Compute our validation losses
                with torch.no_grad():
                    # Compute the example validation loss
                    index_val = np.random.choice(
                        indices_val, args.batch_size, replace=True
                    )
                    x_val = val_data[index_val, :]
                    z_prime, z_prime_jac = net.forward(x_val)
                    example_loss_val = 0.5 * torch.mean(
                        torch.sum(z_prime ** 2, dim=1)
                    ) - torch.mean(z_prime_jac)
                    example_loss_val = args.example_weight * example_loss_val

                    # Compute the energy validation loss
                    z_val = torch.normal(
                        0, 1, size=(args.batch_size, n_dim - 6), device=device
                    )
                    x_prime, x_prime_jac = net.inverse(z_val)
                    val_energies = energy_evaluator(x_prime)
                    energy_loss_val = torch.mean(val_energies) - torch.mean(x_prime_jac)
                    energy_loss_val = args.energy_weight * energy_loss_val

                    # Compute the overall validation loss
                    if args.train_example and args.train_energy:
                        loss_val = example_loss_val + energy_loss_val
                    elif args.train_example:
                        loss_val = example_loss_val
                    else:
                        loss_val = energy_loss_val

                    progress.set_postfix(
                        loss=f"{loss.item():8.3f}", val_loss=f"{loss_val.item():8.3f}"
                    )

                    writer.add_scalar("Validation/loss", loss_val.item(), epoch)
                    writer.add_scalar(
                        "Validation/example", example_loss_val.item(), epoch
                    )
                    writer.add_scalar(
                        "Validation/energy", energy_loss_val.item(), epoch
                    )
                    writer.add_scalar(
                        "Energies/mean_energy", torch.mean(val_energies).item(), epoch
                    )
                    writer.add_scalar(
                        "Energies/median_energy",
                        torch.median(val_energies).item(),
                        epoch,
                    )
                    writer.add_scalar(
                        "Energies/minimum_energy", torch.min(val_energies).item(), epoch
                    )

                    fixed_x, fixed_x_jac = net.inverse(fixed_z)
                    fixed_energy = torch.mean(
                        protein.openmm_energy(fixed_x, openmm_context, args.temperature)
                    )
                    fixed_coords.append(fixed_x.cpu().detach().numpy())
                    writer.add_scalar(
                        "Energies/fixed_energy", fixed_energy.item(), epoch
                    )

    # Save our final model
    torch.save(net, f"models/{args.output_name}.pkl")

    # Log our final losses to the console
    print("Final loss:", loss.item())
    if args.train_example:
        print("Final example loss:", example_loss.item())
    if args.train_energy:
        print("Final energy loss:", energy_loss.item())
    print("Final validation loss:", loss_val.item())
    print("Final validation example loss:", example_loss_val.item())
    print("Final validation energy loss:", energy_loss_val.item())
    print("Final fixed energy loss:", fixed_energy.item())

    # Write the fixed_coords to trajectory
    fixed_coords = np.array(fixed_coords)
    fixed_coords = fixed_coords.reshape(fixed_coords.shape[0], -1, 3)
    traj.unitcell_lengths = None
    traj.unitcell_angles = None
    traj.xyz = fixed_coords
    traj.save(f"training_traj/{args.output_name}.pdb")

    # Generate examples and write trajectory
    net.eval()
    z = torch.normal(0, 1, size=(args.batch_size, n_dim - 6), device=device)
    x, _ = net.inverse(z)
    x = x.cpu().detach().numpy()
    x = x.reshape(args.batch_size, -1, 3)
    traj.xyz = x
    traj.save(f"sample_traj/{args.output_name}.pdb")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="train.py", description="Train generative model of molecular conformation."
    )

    path_group = parser.add_argument_group("paths and filenames")
    # Paths and filenames
    path_group.add_argument("--pdb-path", required=True, help="path to pdb file")
    path_group.add_argument("--dcd-path", required=True, help="path to dcd file")
    path_group.add_argument("--output-name", required=True, help="base name for output")
    path_group.add_argument(
        "--overwrite", action="store_true", help="overwrite previous run"
    )
    path_group.set_defaults(overwrite=False)

    # Optimization parameters
    optimizer_group = parser.add_argument_group("optimization parameters")
    optimizer_group.add_argument(
        "--epochs",
        type=int,
        default=1000,
        help="number of training iterations (default: %(default)d)",
    )
    optimizer_group.add_argument(
        "--batch-size",
        type=int,
        default=1024,
        help="size of training batch (default: %(default)d)",
    )
    optimizer_group.add_argument(
        "--warmup-epochs",
        type=int,
        default=10,
        help="gradually raise learning rate over first WARMUP_EPOCHS (default: %(default)d)",
    )
    optimizer_group.add_argument(
        "--init-lr",
        type=float,
        default=1e-3,
        help="initial learning rate (default: %(default)g)",
    )
    optimizer_group.add_argument(
        "--final-lr",
        type=float,
        default=1e-5,
        help="final learning rate (default: %(default)g)",
    )
    optimizer_group.add_argument(
        "--weight-decay",
        type=float,
        default=1e-3,
        help="strength of weight decay (default: %(default)g)",
    )
    optimizer_group.add_argument(
        "--dropout-fraction",
        type=float,
        default=0.5,
        help="strength of dropout (default: %(default)g)",
    )
    optimizer_group.add_argument(
        "--log-freq",
        type=int,
        default=10,
        help="how often to update tensorboard (default: %(default)d)",
    )
    optimizer_group.add_argument(
        "--fold-validation",
        type=float,
        default=10.0,
        help="how much data to set aside for training (default: %(default)d)",
    )

    # Network parameters
    network_group = parser.add_argument_group("network parameters")
    network_group.add_argument(
        "--load-network", default=None, help="load previously trained network"
    )
    network_group.add_argument(
        "--coupling-layers",
        type=int,
        default=4,
        help="number of coupling layers (%(default)d)",
    )
    network_group.add_argument(
        "--hidden-features",
        type=int,
        default=128,
        help="number of hidden features in each layer (default: %(default)d)",
    )
    network_group.add_argument(
        "--hidden-layers",
        type=int,
        default=2,
        help="number of hidden layers (default: %(default)d)",
    )
    network_group.add_argument(
        "--spline-points",
        type=int,
        default=8,
        help="number of spline points in NSF layers (default: %(default)d)",
    )
    network_group.add_argument(
        "--is-affine",
        action="store_true",
        help="use affine rather than NSF layers (default: False)",
    )
    network_group.set_defaults(is_affine=False)

    # Loss Function parameters
    loss_group = parser.add_argument_group("loss function parameters")
    loss_group.add_argument(
        "--train-example",
        dest="train_example",
        action="store_true",
        help="include training by example in loss (default: True)",
    )
    loss_group.add_argument(
        "--no-train-example", dest="train_example", action="store_false"
    )
    loss_group.set_defaults(train_example=True)
    loss_group.add_argument(
        "--train-energy",
        dest="train_energy",
        action="store_true",
        help="including training by energy in loss (default: False)",
    )
    loss_group.add_argument(
        "--no-train-energy", dest="train_energy", action="store_false"
    )
    loss_group.set_defaults(train_ml=False)
    loss_group.add_argument(
        "--example-weight",
        type=float,
        default=1.0,
        help="weight for training by example (default: %(default)g)",
    )
    loss_group.add_argument(
        "--energy-weight",
        type=float,
        default=1.0,
        help="weight for training by energy (default: %(default)g)",
    )

    # Energy evaluation parameters
    energy_group = parser.add_argument_group("parameters for energy function")
    energy_group.add_argument(
        "--temperature",
        type=float,
        default=298.0,
        help="temperature (default: %(default)g)",
    )
    energy_group.add_argument(
        "--energy-max",
        type=float,
        default=1e20,
        help="maximum energy (default: %(default)g)",
    )
    energy_group.add_argument(
        "--energy-high",
        type=float,
        default=1e10,
        help="log transform energies above this value (default: %(default)g)",
    )

    args = parser.parse_args()

    if not (args.train_example or args.train_energy):
        raise RuntimeError(
            "You must specify at least one of train_example or train_energy."
        )

    model_path = f"models/{args.output_name}.pkl"
    if os.path.exists(model_path):
        if args.overwrite:
            print(f"Warning: output `{model_path}' already exists. Overwriting anyway.")
        else:
            raise RuntimeError(
                f"Output '{model_path}' already exists. If you're sure use --overwrite."
            )

    # Remove any old data for this run
    delete_run(args.output_name)

    create_dirs()
    device = get_device()
    run_training(args, device)

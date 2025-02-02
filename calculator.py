# Standard imports
import torch
import numpy as np
import argparse
#ASE importations
from ase.calculators.calculator import Calculator
from ase.neighborlist import neighbor_list
#Neural network imports
from Neural_Net import PhysNet
from .layers.activation_fn import  *
''' 
Calculator for the atomic simulation environment (ASE) 
that evaluates energies and forces using a neural network.
'''

class PhysNetCalculator(Calculator):
    implemented_properties = ['energy', 'energy_and_uncertainty', 'forces','hessian']
    def __init__(self,
                 # ASE atoms object
                 atoms,
                 # ckpt file to restore the model (can also be a list for ensembles)
                 checkpoint,
                 # Respective config file for PhysNet architecture
                 config,
                 # System charge
                 charge=0,
                 # Cutoff distance for long range interactions (default: no cutoff)
                 lr_cut = None,
                 # Activation function
                 activation_fn="shift_softplus",
                 hessian=False,
                 # Single or double precision
                 dtype=torch.float64):
        # Read config file to ensure same PhysNet architecture as during fit
        # Initiate parser
        parser = argparse.ArgumentParser(fromfile_prefix_chars='@')

        # Add arguments
        parser.add_argument("--restart", type=str, default='No',
                            help="Restart training from a specific folder")
        parser.add_argument("--num_features", default=128, type=int)
        parser.add_argument("--num_basis", default=64, type=int)
        parser.add_argument("--num_blocks", default=5, type=int)
        parser.add_argument("--num_residual_atomic", default=2, type=int)
        parser.add_argument("--num_residual_interaction", default=3, type=int)
        parser.add_argument("--num_residual_output", default=1, type=int)
        parser.add_argument("--cutoff", default=10.0, type=float)
        parser.add_argument("--use_electrostatic", default=1, type=int)
        parser.add_argument("--use_dispersion", default=1, type=int)
        parser.add_argument("--grimme_s6", default=None, type=float)
        parser.add_argument("--grimme_s8", default=None, type=float)
        parser.add_argument("--grimme_a1", default=None, type=float)
        parser.add_argument("--grimme_a2", default=None, type=float)
        parser.add_argument("--dataset", type=str)
        parser.add_argument("--num_train", type=int)
        parser.add_argument("--num_valid", type=int)
        parser.add_argument("--batch_size", type=int)
        parser.add_argument("--valid_batch_size", type=int)
        parser.add_argument("--seed", default=None, type=int)
        parser.add_argument("--max_steps", default=10000, type=int)
        parser.add_argument("--learning_rate", default=0.001, type=float)
        parser.add_argument("--decay_steps", default=1000, type=int)
        parser.add_argument("--decay_rate", default=0.1, type=float)
        parser.add_argument("--max_norm", default=1000.0, type=float)
        parser.add_argument("--ema_decay", default=0.999, type=float)
        parser.add_argument("--rate", default=0.0, type=float)
        parser.add_argument("--l2lambda", default=0.0, type=float)
        parser.add_argument("--nhlambda", default=0.1, type=float)
        parser.add_argument("--lambda_conf",default=0.2,type=float)
        parser.add_argument("--summary_interval", default=5, type=int)
        parser.add_argument("--validation_interval", default=5, type=int)
        parser.add_argument("--show_progress", default=True, type=bool)
        parser.add_argument("--save_interval", default=5, type=int)
        parser.add_argument("--record_run_metadata", default=0, type=int)
        parser.add_argument('--device', default='cuda', type=str)
        parser.add_argument('--DER_type',default=None,type=str)

        # Read config file
        args = parser.parse_args(["@" + config])
        # Create neighborlist
        if lr_cut is None:
            self._sr_cutoff = args.cutoff
            self._lr_cutoff = None
            self._use_neighborlist = False
        else:
            self._sr_cutoff = args.cutoff
            self._lr_cutoff = lr_cut
            self._use_neighborlist = True

        # Periodic boundary conditions
        self.pbc = atoms.pbc
        self.cell = atoms.cell.diagonal()

        # Set up device
        self.device = args.device
        # Set up hessian flag
        self.hessian_active = hessian
        # Set up DER type
        self.DER_type = args.DER_type

        # Initiate calculator
        Calculator.__init__(self)

        # Set checkpoint file(s)
        self._checkpoint = checkpoint
        # Create PhysNet model
        self._model = PhysNet(
            F=args.num_features,
            K=args.num_basis,
            sr_cut=args.cutoff,
            num_blocks=args.num_blocks,
            num_residual_atomic=args.num_residual_atomic,
            num_residual_interaction=args.num_residual_interaction,
            num_residual_output=args.num_residual_output,
            use_electrostatic=(args.use_electrostatic == 1),
            use_dispersion=(args.use_dispersion == 1),
            s6=args.grimme_s6,
            s8=args.grimme_s8,
            a1=args.grimme_a1,
            a2=args.grimme_a2,
            writer=False,
            activation_fn=shifted_softplus,
            device=args.device)

        self._Z = torch.tensor(atoms.get_atomic_numbers(), dtype=torch.int32,device=self.device)
        self._R = torch.tensor(atoms.get_positions(), dtype=torch.float32,requires_grad=True,device=self.device)
        self._Q_tot = torch.tensor([charge],dtype=dtype,device=self.device)
        self._idx_i, self._idx_j = self.get_indices(atoms,device=self.device)

        # Initiate Embedded flag
        # self.pcpot = None
        def load_checkpoint(path):
            if path is not None:
                checkpoint = torch.load(path)
                return checkpoint
        # Load neural network parameter
        latest_ckpt = load_checkpoint(self.checkpoint)
        self._model.load_state_dict(latest_ckpt['model_state_dict'])
        self._model.eval()
        self._last_atoms = None
        # Calculate properties once to initialize everything
        # self._calculate_all_properties(atoms)
        # self.calculation_required(atoms)
        self.calculate(atoms,properties=self.implemented_properties)
        # Set last_atoms to None as pcpot get enabled later and recalculation
        # becomes necessary again
        self._last_atoms = None
        Calculator.__init__(self)


    def get_indices(self, atoms,device='cpu'):
        # Number of atoms
        N = len(atoms)
        # Indices pointing to atom at each batch image
        idx = torch.arange(end=N,dtype=torch.int32).to(device)
        # Indices for atom pairs ij - Atom i
        idx_i = idx.repeat(int(N) - 1)
        # Indices for atom pairs ij - Atom j
        idx_j = torch.roll(idx, -1, dims=0)

        if N>=2:
            for Na in torch.arange(2, N):
                Na_tmp = Na.cpu()
                idx_j = torch.concat(
                    [idx_j, torch.roll(idx, int(-Na_tmp.numpy()), dims=0)],
                    dim=0)
        idx_i = torch.sort(idx_i)[0]
        return idx_i.type(torch.int64), idx_j.type(torch.int64)

    def calculation_required(self, atoms):

        # Check positions, atomic numbers, unit cell and pbc
        if self.last_atoms is None:
            return True
        else:
            return atoms != self.last_atoms

    def calculate(self, atoms, properties=None, system_changes=None):
        # find neighbors and offsets

        if self.use_neighborlist or any(atoms.get_pbc()):

            idx_i, idx_j, S = neighbor_list('ijS', atoms, self.lr_cutoff)
            offsets = np.dot(S, atoms.get_cell())
            sr_idx_i, sr_idx_j, sr_S = neighbor_list(
                'ijS', atoms, self.sr_cutoff)
            sr_offsets = np.dot(sr_S, atoms.get_cell())

        else:

            idx_i = self.idx_i
            idx_j = self.idx_j
            offsets = None
            sr_idx_i = None
            sr_idx_j = None
            sr_offsets = None

        # Calculate energy
        # (in case multiple NNs are used as ensemble, take the average)
        # Only one NN

        self.model.eval()
        self._R = torch.tensor(atoms.get_positions(), dtype=torch.float32,requires_grad=True,device=self.device)

        if self.DER_type == 'simple' or 'Lipz':
            if self.hessian_active:
                self._last_energy, lambdas, alpha, beta, self._last_charges, self._last_forces, self._last_hessian = \
                    self.model.energy_forces_and_hessian_evidential(self.Z, self.R, idx_i, idx_j, Q_tot=self.Q_tot,
                                                                    batch_seg=None,
                                                                    offsets=offsets, sr_idx_i=sr_idx_i,
                                                                    sr_idx_j=sr_idx_j,
                                                                    sr_offsets=sr_offsets)
            else:
                self._last_energy, lambdas, alpha, beta, self._last_charges, self._last_forces = \
                    self.model.energy_forces_and_others_evidential(self.Z, self.R, idx_i, idx_j, Q_tot=self.Q_tot,
                                                                   batch_seg=None,
                                                                   offsets=offsets, sr_idx_i=sr_idx_i,
                                                                   sr_idx_j=sr_idx_j,
                                                                   sr_offsets=sr_offsets)

            self._sigma2 = beta.detach().cpu().numpy()/(alpha.detach().cpu().numpy()-1)
            self._var = (1/lambdas.detach().cpu().numpy())*self.sigma2

            # Convert results to numpy array
            self._last_energy = self._last_energy.detach().cpu().numpy()
            self._last_forces = self._last_forces.clone().detach().cpu().numpy()

            if self.hessian_active:
               self._last_hessian = self._last_hessian.clone().detach().cpu().numpy()
            else:
               self._last_hessian = None

        elif self.DER_type == 'MD':
            if self.hessian_active:
                pred, Dip, self._last_forces, self._last_hessian = \
                    self.model.energy_hessian_and_forces_md_evidencial(self.Z, self.R, idx_i, idx_j, Q_tot=self.Q_tot,
                                                                       batch_seg=None)
            else:
                pred, Dip, self._last_forces = \
                    self.model.energy_and_forces_md_evidencial(self.Z, self.R, idx_i, idx_j, Q_tot=self.Q_tot,
                                                               batch_seg=None)

            # Predictions from the NN, first index is the energy second index is the charges
            mu = [pred[0].detach().cpu().numpy(), pred[1].detach().cpu().numpy()]

            # Uncertainty
            L = torch.zeros((2, 2), device=self.device)
            L[0, 0] = pred[2]
            L[1, 0] = pred[3]
            L[1, 1] = pred[4]
            sigma = torch.matmul(L, L.transpose(1, 0))

            nu = pred[5].detach().cpu().numpy()

            sigma2 = nu / (nu - 3) * sigma
            var = 1 / nu * sigma2

            self._last_energy = mu[0]
            self._sigma2 = sigma[0, 0].detach().cpu().numpy()
            self._var = var[0, 0].detach().cpu().numpy()

            # Convert results to numpy array
            self._last_energy = self._last_energy
            self._last_forces = self._last_forces.clone().detach().cpu().numpy()
            if self.hessian_active:
                self._last_hessian = self._last_hessian.clone().detach().cpu().numpy()
            else:
                self._last_hessian = None

        # prevents some problems... but not for me, it actually does one
        # self._last_energy = np.array(1*[self.last_energy])
        # Store a copy of the atoms object


        # Store results in results dictionary
        self.results['energy'] = self.last_energy
        self.results['forces'] = self.last_forces
        self.results['hessian'] = self.last_hessian
        self._last_atoms = atoms.copy()



    def get_potential_energy(self, atoms, force_consistent=False):

        if self.calculation_required(atoms):
            self.calculate(atoms)
        return self.results['energy']

    def get_potential_energy_and_uncertainty(self, atoms):

        if self.calculation_required(atoms):
            self.calculate(atoms)

        return self.last_energy, self.variance, self.sigma2

    def get_potential_energy_uncertainty_and_forces(self, atoms):
        if self.calculation_required(atoms):
            self.calculate(atoms)

        return self.last_energy, self.variance, self.sigma2, self.last_forces

    def get_forces(self,atoms):
        if self.calculation_required(atoms):
            self.calculate(atoms)

        return self.last_forces

    def get_hessian(self, atoms):
        if self.calculation_required(atoms):
            self.calculate(atoms)
        return self.last_hessian

    @property
    def last_atoms(self):
        return self._last_atoms

    @property
    def last_energy(self):
        return self._last_energy

    @property
    def last_forces(self):
        return self._last_forces

    @property
    def last_hessian(self):
        return self._last_hessian


    @property
    def variance(self):
        return self._var

    @property
    def sigma2(self):
        return self._sigma2

    @property
    def sr_cutoff(self):
        return self._sr_cutoff

    @property
    def lr_cutoff(self):
        return self._lr_cutoff

    @property
    def use_neighborlist(self):
        return self._use_neighborlist

    @property
    def model(self):
        return self._model

    @property
    def checkpoint(self):
        return self._checkpoint

    @property
    def Z(self):
        return self._Z

    @property
    def Q_tot(self):
        return self._Q_tot

    @property
    def R(self):
        return self._R

    @property
    def idx_i(self):
        return self._idx_i

    @property
    def idx_j(self):
        return self._idx_j

    @property
    def energy(self):
        return self._energy

    @property
    def forces(self):
        return self._forces

    @property
    def hessian(self):
        return self._hessian

    @property
    def energy_and_uncertainty(self):
        return self._energy_and_uncertainty

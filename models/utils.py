import argparse
import json
import os
import re
import sys
from typing import Literal, Sequence

import numpy as np
import pandas as pd
import pymol
import torch
import torch.nn.functional as F
from Bio.Align import substitution_matrices
from Bio.PDB import PDBParser, is_aa
from pymol import cmd
from scipy.spatial import distance_matrix
from tqdm.auto import tqdm

from models.globals import AA_ALPHABET, AA_DICT, PDB_CHAIN_IDS


def _normalize_submat(submat: np.ndarray) -> np.ndarray:
    assert len(submat.shape) == 2
    assert submat.shape[0] == submat.shape[1] == 20

    diag_sum = np.diag(submat).sum()
    tril_sum = np.tril(submat, k=-1).sum()
    diag_num = submat.shape[0]
    tril_num = diag_num * (diag_num - 1) // 2

    lambda_, mu_ = (
        np.array([diag_num, -diag_sum])
        * tril_num
        / (diag_num * tril_sum - tril_num * diag_sum)
    )
    submat_norm = lambda_ * submat + mu_
    assert np.isclose(np.diag(submat_norm).sum(), 0)
    np.fill_diagonal(submat_norm, 0)
    assert np.isclose(submat_norm.sum(), 380)

    return submat_norm


def count_mutations(
    seqs: list[str], seq0: str, substitution_matrix: str = "identity"
) -> np.ndarray:
    """Count the number of mutations between sequences and a reference sequence of the same length.

    Args
    ----
    seqs : list[str]
        List of sequences to compare.
    seq0 : str
        Reference sequence.
    substitution_matrix : str
        Substitution matrix to use. Can be "identity" or a name from Bio.Align.substitution_matrices.load().

    Returns
    -------
    mutation_counts : np.ndarray
        Number of mutations between each sequence and the reference sequence.
    """

    if substitution_matrix == "identity":
        submat: np.ndarray = np.identity(20)
    elif substitution_matrix in substitution_matrices.load():
        submat_fromBio = substitution_matrices.load(substitution_matrix)
        if not set(AA_ALPHABET).issubset(set(submat_fromBio.alphabet)):
            raise ValueError(
                f"Substitution matrix {substitution_matrix} does not contain all 20 amino acids."
            )
        submat: np.ndarray = (
            pd.DataFrame(
                submat_fromBio,
                index=list(submat_fromBio.alphabet),
                columns=list(submat_fromBio.alphabet),
            )
            .loc[list(AA_ALPHABET), list(AA_ALPHABET)]
            .to_numpy()
        )
    else:
        raise ValueError(f"Unknown substitution matrix: {substitution_matrix}")

    submat_norm = _normalize_submat(submat)

    try:
        seqs_idx = np.array([[AA_DICT[aa] for aa in seq] for seq in seqs])
        seq0_idx = np.array([AA_DICT[aa] for aa in seq0])
    except KeyError as e:
        raise KeyError(f"Invalid amino acid: {e}") from None
    except ValueError:
        raise ValueError("Some sequences have different lengths.") from None

    return submat_norm[seqs_idx, seq0_idx].sum(axis=1)


def parse_fasta(fasta_file: str, idx: str = "id", prefix: str = "") -> pd.DataFrame:
    """Parse a fasta file into a pandas DataFrame.

    Args
    ----
    fasta_file : str
        Path to the fasta file.
    idx : str
        Key in the title line to add a prefix.
    prefix : str
        Prefix to add to a specific value of the title key.

    Returns
    -------
    seqs_info : pd.DataFrame
        DataFrame with columns from the title line and the sequence.
    """

    title_regex = r"^>(\w+=[\w\d_.]+)(, \w+=[\w\d_.]+)*$"
    sequence_regex = r"^[ACDEFGHIKLMNPQRSTVWY:]+$"

    with open(fasta_file) as f:
        lines = f.readlines()

    titles = []
    sequences = []
    j = 0
    for i, line in enumerate(lines):
        if re.match(title_regex, line):
            title = line.strip()[1:]
            titles.append(title)
            if i - j:
                raise ValueError(f"Line {line} does not match fasta format")
        elif re.match(sequence_regex, line):
            sequence = line.strip()
            sequences.append(sequence)
            if i - j - 1:
                raise ValueError(f"Line {line} does not match fasta format")
            j = i + 1
        else:
            raise ValueError(f"Line {line} does not match title or sequence regex")

    if len(titles) != len(sequences):
        raise ValueError("Number of titles and sequences do not match")

    title_dicts = []
    for title, sequence in zip(titles, sequences):
        title_dict = {}
        for title_part in title.split(","):
            if title_part:
                key, value = title_part.strip().split("=")
                if key == idx:
                    value = prefix + value
                title_dict[key] = value
        title_dict["sequence"] = sequence
        title_dicts.append(title_dict)

    return pd.DataFrame(title_dicts)


def get_top_percentile(
    df: pd.DataFrame,
    columns: list[str],
    percentile: float = 0.5,
    ascending: bool = True,
    ignore_index=False,
) -> pd.DataFrame:
    """Get top percentile of dataframe based on columns."""
    if ascending:
        df_copy = df[
            (df[columns].rank(method="dense", pct=True) <= percentile).all(axis=1)
        ]
    else:
        df_copy = df[
            (df[columns].rank(method="dense", pct=True) >= percentile).all(axis=1)
        ]

    if ignore_index:
        df = df.reset_index(drop=True)

    return df_copy


def calculate_distance_matrix(pdb_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Calculate the distance matrix of a PDB file."""
    structure = PDBParser(QUIET=True).get_structure("pdb", pdb_path)
    if len(structure) != 1:
        raise ValueError("Structure contains more than one model.")
    model_ = next(structure.get_models())

    coords_list = []
    mask_list = []
    for chain in model_:
        for residue in chain:
            if not is_aa(residue):
                print(f"Skipping {residue.resname}")
                continue

            mask_list.append(chain.id)
            if residue.resname in {"ALA", "GLY"}:
                coords_list.append(residue["CA"].coord)
            else:
                coords_list.append(residue["CB"].coord)
    coords = np.array(coords_list)
    mask = np.array(mask_list)

    return distance_matrix(coords, coords), mask


def extract_from_esmfold(file_path: str) -> pd.Series:
    """Extract information from an ESMFold output .pt file."""
    data = torch.load(file_path, weights_only=True)

    atom37_atom_exists = data["atom37_atom_exists"].cpu().squeeze(0)
    linker_mask = torch.any(atom37_atom_exists, dim=-1)

    atom37_atom_exists = atom37_atom_exists[linker_mask]
    plddt = data["plddt"].cpu().squeeze(0)[linker_mask]
    ptm = data["ptm"].cpu().item()
    predicted_aligned_error = (
        data["predicted_aligned_error"].cpu().squeeze(0)[linker_mask][:, linker_mask]
    )
    mean_plddt = data["mean_plddt"].cpu().item()
    chain_index = data["chain_index"].cpu().squeeze(0)[linker_mask]

    chain_index_one_hot = F.one_hot(chain_index).bool().T
    chain_num = chain_index_one_hot.shape[0]

    pt_dict = {}
    pt_dict["id"] = os.path.splitext(os.path.basename(file_path))[0]
    pt_dict["plddt"] = mean_plddt
    pt_dict["pae"] = torch.mean(predicted_aligned_error).item()
    pt_dict["ptm"] = ptm

    for i in range(chain_num):
        chain_id_1 = PDB_CHAIN_IDS[i]
        chain_mask_1 = chain_index_one_hot[i]
        res_num_1 = torch.sum(chain_mask_1)
        mean_plddt_1 = torch.sum(
            plddt[chain_mask_1] * atom37_atom_exists[chain_mask_1]
        ) / torch.sum(atom37_atom_exists[chain_mask_1])
        pt_dict[f"plddt_{chain_id_1}"] = mean_plddt_1.item()

        for j in range(i, chain_num):
            chain_id_2 = PDB_CHAIN_IDS[j]
            chain_mask_2 = chain_index_one_hot[j]
            res_num_2 = torch.sum(chain_mask_2)
            pae_interaction_1_2 = (
                torch.sum(predicted_aligned_error[chain_mask_1][:, chain_mask_2])
                + torch.sum(predicted_aligned_error[chain_mask_2][:, chain_mask_1])
            ) / (2 * res_num_1 * res_num_2)
            pt_dict[f"pae_interaction_{chain_id_1}_{chain_id_2}"] = (
                pae_interaction_1_2.item()
            )

    return pd.Series(pt_dict)


class PyMOLSession:
    """Context manager for a PyMOL session.

    Example
    -------
    >>> with PyMOLSession():
    >>>     cmd.load("structure.pdb", "structure")
    """

    def __enter__(self):
        cmd.reinitialize()  # Clean up the PyMOL session
        pymol.finish_launching(["pymol", "-cq"])  # Launch PyMOL in headless mode

    def __exit__(self, exc_type, exc_value, traceback):
        cmd.reinitialize()  # Clean up the PyMOL session


def calculate_rmsd(
    mobile: str,
    target: str,
    how: Literal["align", "super"] = "align",
    on: str = "all",
    reports: str | Sequence[str] = "all",
    **kwargs,
) -> list[float]:
    """Calculate the RMSD between the mobile structure and the target structure.

    Args
    ----
    mobile : str
        The path to the mobile structure.
    target : str
        The path to the target structure.
    how : Literal["align", "super"]
        The method to calculate the RMSD. Default is "align".
        align: using a sequence alignment followed by a structural superposition (for sequence similarity > 30%)
        super: using a sequence-independent structure-based superposition (for low sequence similarity)
    on : str
        The selection to align/superimpose the structures. Default is "all".
    reports : str | Sequence[str]
        The selection to calculate the RMSD. Default is "all".
    **kwargs
        Additional keyword arguments for `cmd.align` or `cmd.super`.

    Returns
    -------
    rmsds : list[float]
        The RMSDs between the mobile structure and the target structure for each report selection.
    """

    with PyMOLSession():

        # Set the report selections
        if isinstance(reports, str):
            reports = [reports]

        # Set the alignment/superimposition method
        if how == "align":
            func = cmd.align
        elif how == "super":
            func = cmd.super
        else:
            raise ValueError(f"Invalid method: {how}")

        # Load the structures
        cmd.load(target, "target")
        cmd.load(mobile, "mobile")

        # Align/superimpose the structures
        func(f"mobile and {on}", f"target and {on}", **kwargs)

        rmsds = []
        for i, report in enumerate(reports):
            # Create the alignment object without touching the structures
            func(
                f"mobile and {report}",
                f"target and {report}",
                cycles=0,
                transform=0,
                object=f"aln_{i}",
            )
            # Calculate the RMSD between the matched atoms
            rmsd = cmd.rms_cur(
                f"mobile and {report} and aln_{i}",
                f"target and {report} and aln_{i}",
                matchmaker=-1,
            )
            rmsds.append(rmsd)

    return rmsds

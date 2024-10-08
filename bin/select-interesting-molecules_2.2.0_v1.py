#!/usr/bin/env python3
"""
Date generated: 2024-09-17

This one-file script takes an input of molecules in SMILES format
and selects molecules with chemistries where we are seeking to improve data coverage.

The rare environments include a list of functional groups and openff-2.2.0.offxml parameters
for which there is low coverage in our already available data.

"""

import argparse
import contextlib
import functools
import multiprocessing
import pathlib
import tempfile

from openff.toolkit.topology import Molecule
from openff.toolkit.typing.engines.smirnoff import ForceField
import pandas as pd

parser = argparse.ArgumentParser(
    description=(
        "Select molecules with chemistries where we are seeking to improve data coverage. "
        "This takes in a multi-molecule SMILES file and outputs a multi-molecule SMILES file, "
        "where each molecule is on a separate line. \n"
        "The chemistries we are selecting for include a list of functional groups "
        "and openff-2.2.0.offxml parameters for which there is low coverage in our already available data."
    ),
    formatter_class=argparse.RawDescriptionHelpFormatter  # preserve newlines
)
parser.add_argument(
    "-i", "--input",
    type=str,
    help="Path to a file containing SMILES strings, with one on each line.",
    required=True
)
parser.add_argument(
    "-o", "--output",
    type=str,
    default="output.smi",
    help="Path to the output SMILES file. (Default: output.smi)",
    required=False
)
parser.add_argument(
    "-n", "--only-top-n",
    type=int,
    default=-1,
    help=(
        "Only write the top N molecules to the output file. "
        "If not specified, write all molecules."
    ),
    required=False
)
parser.add_argument(
    "-np", "--nproc",
    type=int,
    default=1,
    help="Number of processes to use. (Default: 1)",
    required=False
)
parser.add_argument(
    "-oc", "--output-count",
    type=str,
    default=None,
    help=(
        "If specified, write the counts of each group as a CSV to the given path. "
    )
)
parser.add_argument(
    "-of", "--output-full",
    type=str,
    default=None,
    help=(
        "If specified, write matches to low coverage groups as a CSV to the given path. "
        "Each group will be a column, with boolean values to indicate if this group is "
        "present in the molecule. "
        "Each row will correspond to a molecule in the input file. "
        "A column 'Count' will be included to indicate the total number of matches. "
        "If not specified, this file will not be written."
    ),
    required=False
)
parser.add_argument(
    "-c", "--count-threshold",
    type=int,
    default=1,
    help=(
        "Number of matches to groups with low existing data coverage. "
        "Only molecules with a count greater than or equal to this threshold "
        "will be written as output. (Default: 1)"
    ),
    required=False
)



def main():
    args = parser.parse_args()
    with open(args.input) as f:
        smiles = [line.strip() for line in f]
    smiles = [line for line in smiles if line]
    search_all_smiles(
        smiles,
        args.output,
        nprocs=args.nproc,
        output_csv_file=args.output_full,
        output_count_file=args.output_count,
        count_threshold=args.count_threshold,
        only_top_n=args.only_top_n,
    )


def draw_checkmol():
    """
    Draw the checkmol SMARTS for verification.
    
    This is not used in the main script, but can be used to generate a
    visual representation of the checkmol groups.
    """
    from rdkit import Chem
    from rdkit.Chem import Draw

    rdmols = [
        Chem.MolFromSmarts(smirks)
        for smirks in CHECKMOL_GROUPS.values()
    ]
    img = Draw.MolsToGridImage(
        rdmols,
        molsPerRow=4,
        subImgSize=(300, 300),
        legends=list(CHECKMOL_GROUPS.keys()),
    )
    img.save("checkmol.png")


def cast_or_error(value, type_, var_name):
    try:
        return type_(value)
    except ValueError:
        raise ValueError(f"Could not cast {var_name} to {type_}: {value}")


def search_all_smiles(
    smiles,
    output_file: str,
    nprocs: int = 1,
    output_csv_file: str = None,
    output_count_file: str = None,
    count_threshold: int = 1,
    only_top_n: int = -1,
):
    """
    Search all SMILES strings for rare environments and write to a CSV file.

    Parameters
    ----------
    smiles : list[str]
        List of SMILES strings to search.
    output_file : str
        Path to the output CSV file.
    nprocs : int, optional
        Number of processors to use, by default 1.
    count_threshold : int, optional
        Threshold for considering a parameter 'rare', by default 1.
    only_top_n : int, optional
        Only write the top N entries to the output file. Default -1.
    output_csv_file : str, optional
        Path to the output CSV file. If not specified, this file will not be written.
    output_count_file : str, optional
        Path to the output CSV file. If not specified, this file will not be written.
    """

    # check inputs
    output_file = pathlib.Path(str(output_file))
    output_file.parent.mkdir(exist_ok=True, parents=True)
    if output_csv_file:
        output_csv_file = pathlib.Path(str(output_csv_file))
        output_csv_file.parent.mkdir(exist_ok=True, parents=True)
    if output_count_file:
        output_count_file = pathlib.Path(str(output_count_file))
        output_count_file.parent.mkdir(exist_ok=True, parents=True)
    
    nprocs = cast_or_error(nprocs, int, "-np/--nproc")
    count_threshold = cast_or_error(count_threshold, int, "-c/--count-threshold")
    only_top_n = cast_or_error(only_top_n, int, "-n/--only-top-n")
    if only_top_n == 0:
        raise ValueError("-n/--only-top-n cannot be 0")

    forcefield = load_forcefield()
    empty_entry = { group: False for group in CHECKMOL_GROUPS }
    for parameter_id in LOW_COVERAGE_PARAMETERS:
        empty_entry[parameter_id] = False
    all_entries = []

    labeller = functools.partial(
        label_single_smiles,
        forcefield=forcefield,
        empty_entry=empty_entry,
    )
    with multiprocessing.Pool(nprocs) as pool:
        all_entries = list(
            _progress_bar(
                pool.imap(labeller, smiles),
                desc="Matching molecules",
                total=len(smiles),
            )
        )
    all_entries = [x for x in all_entries if x is not None]
    if not len(all_entries):
        print(f"No valid matches found -- skipping writing to {output_file}")
        return

    all_entries = sorted(all_entries, key=lambda x: x["Count"], reverse=True)
    df = pd.DataFrame(all_entries)
    df = df[df["Count"] >= count_threshold]

    if only_top_n > 0:
        df = df.head(only_top_n)

    with open(output_file, "w") as f:
        f.write("\n".join(df["SMILES"].values))
    print(f"Wrote {len(df)} molecules to {output_file}")

    core_keys = ["SMILES", "Count"]
    added_keys = [col for col in df.columns if col not in core_keys]
    if output_count_file:
        counts = df[added_keys].sum()
        counts.to_csv(output_count_file, header=False)
        print(f"Wrote counts to {output_count_file}")

    if output_csv_file:
        keys = core_keys + added_keys
        df[keys].to_csv(output_csv_file, index=False)
        n_groups = len(df.columns) - 2
        print(f"Wrote {len(df)} molecules and matches to {n_groups} groups to {output_csv_file}")

    

def _progress_bar(iterable_, **kwargs):
    """Try to use tqdm if it is available, otherwise return the iterable."""
    try:
        import tqdm
        return tqdm.tqdm(iterable_, **kwargs)
    except ImportError:
        return iterable_

def load_forcefield():
    """Load the OpenFF 2.2.0 force field from the string below."""
    # write force field to temp file and load
    with tempfile.NamedTemporaryFile("w") as f:
        f.write(FORCEFIELD)
        forcefield = ForceField(f.name)
    return forcefield

def label_single_smiles(
    smi: str,
    forcefield: ForceField,
    empty_entry,
):
    """Label a single SMILES string with rare environments."""
    # ignore warnings about stereo
    with capture_toolkit_warnings():
        try:
            mol = Molecule.from_smiles(smi, allow_undefined_stereo=True)
        except Exception as e:
            return None
    
    atomic_numbers = [atom.atomic_number for atom in mol.atoms]
    if 0 in atomic_numbers:
        return None

    entry = dict(empty_entry)

    # does it match any checkmol groups?
    for group, smirks in CHECKMOL_GROUPS.items():
        matches = mol.chemical_environment_matches(smirks)
        if len(matches):
            entry[group] = True

    labels = forcefield.label_molecules(mol.to_topology())[0]
    for parameters in labels.values():
        for parameter in parameters.values():
            label = parameter.id if parameter.id else parameter.name
            if label in entry:
                entry[label] = True

    entry["Count"] = sum(entry.values())
    entry["SMILES"] = smi
    return entry

@contextlib.contextmanager
def capture_toolkit_warnings(run: bool = True):  # pragma: no cover
    """A convenience method to capture and discard any warning produced by external
    cheminformatics toolkits excluding the OpenFF toolkit. This should be used with
    extreme caution and is only really intended for use when processing tens of
    thousands of molecules at once."""

    import logging
    import warnings

    if not run:
        yield
        return

    warnings.filterwarnings("ignore")

    toolkit_logger = logging.getLogger("openff.toolkit")
    openff_logger_level = toolkit_logger.getEffectiveLevel()
    toolkit_logger.setLevel(logging.ERROR)

    yield

    toolkit_logger.setLevel(openff_logger_level)






#  ____                                                           
# |  _ \__ _ _ __ ___                                            
# | |_) / _` | '__/ _ \                                           
# |  _ < (_| | | |  __/                                           
# |_| \_\__,_|_|  \___|                                   _       
#   ___ _ ____   _(_)_ __ ___  _ __  _ __ ___   ___ _ __ | |_ ___ 
#  / _ \ '_ \ \ / / | '__/ _ \| '_ \| '_ ` _ \ / _ \ '_ \| __/ __|
# |  __/ | | \ V /| | | | (_) | | | | | | | | |  __/ | | | |_\__ \
#  \___|_| |_|\_/ |_|_|  \___/|_| |_|_| |_| |_|\___|_| |_|\__|___/



# If updating this script, this section should be updated as well.

# checkmol groups:
# these were identified using the checkmol software.
CHECKMOL_GROUPS = {
    "Acyl Bromide": "[#35:1]-[#6X3:2](=O)[#6,#1]",  # 0 matches
    "Acyl Fluoride": "[#9:1]-[#6X3:2](=O)[#6,#1]",  # 0 matches
    "Acyl Iodide": "[#53:1]-[#6X3:2](=O)[#6,#1]",  # 0 matches
    "Carbodiimide": "[#1,#6]-[#7X2:1]=[#6X2:2]=[#7X2:3]-[#1,#6]",  # 0 matches
    "Organolithium": "[#6:1]-[#3:2]",  # 0 matches
    "Organomagnesium": "[#6:1]-[#12:2]",  # 0 matches
    "Organometallic": "[#6:1]-[#3,#12:2]",  # 0 matches
    "Sulfenic Acid Halide": "[#6:1]-[#16X2:2]-[#9,#17,#35,#53:3]",  # 0 matches
    "Thiocarbonic Acid Ester Halide": "[#6X3:1](=[#16X1:2])(-[#8X2:3]-[#6])-[#9,#17,#35,#53:4]",  # 0 matches
    "Thiophosphoric Acid": "[#15:1](=[#16X1:2])(-[#8X2]-[!#6])(-[#8X2]-[!#6])-[#8X2]-[!#6]",  # 0 matches
    "Thiophosphoric Acid Halide": "[#15:1](=[#16X1:2])(-[#9,#17,#35,#53:3])(-[#8,#7,#9,#17,#35,#53])-[#8,#7,#9,#17,#35,#53]",  # 0 matches
    "Carbamic Acid Halide": "[#6X3:1](=[#8X1:2])(-[#9,#17,#35,#53:3])-[#7X3:4](-[#1,#6])-[#1,#6]",  # 1 matches
    "Carbonic Acid Ester Halide": "[#6X3:1](=[#8X1:2])(-[#8X2:3]-[#6])-[#9,#17,#35,#53:4]",  # 1 matches
    "Carboxylic Acid Azide": "[#6]-[#6X3:1](=[#8X1:2])-[#7X2:3]=[#7X2+1:4]=[#7X1-1:5]",  # 1 matches
    "Cyanate": "[#6]-[#8X2:1]-[#6X2:2]#[#7X1:3]",  # 1 matches
    "Nitrite": "[#1,#6]-[#8X2:1]-[#7X2:2]=[#8X1:3]",  # 1 matches
    "Thiocarbamic Acid Halide": "[#6X3:1](=[#16X1:2])(-[#9,#17,#35,#53:3])-[#7X3:4](-[#1,#6])-[#1,#6]",  # 1 matches
    "Thiosemicarbazide": "[#1,#6]-[#7X3:1](-[#1,#6])-[#6X3:2](=[#16X1:3])-[#7X3:4](-[#1,#6])-[#7X3:5](-[#1,#6])-[#1,#6]",  # 1 matches
    "Thiosemicarbazone": "[#1,#6]-[#6X3:1](-[#1,#6])=[#7X2:2]~[#7:3](-[#1,#6])-[#6X3](=[#16X1])-[#7X3](-[#1,#6])-[#1,#6]",  # 1 matches
    "Acyl Cyanide": "[#1,#6]-[#6X3:1](=[#8X1:2])-[#6X2:3]#[#7X1:4]",  # 2 matches
    "Phosphoric Acid": "[#15:1](=[#8X1:2])(-[#8X2]-[!#6])(-[#8X2]-[!#6])-[#8X2]-[!#6]",  # 2 matches
    "Sulfenic Acid Ester": "[#6:1]-[#16X2:2]-[#8X2:3]-[#6:4]",  # 2 matches
    "Sulfinic Acid Halide": "[#1,#6]-[#16X3:1](=[#8X1:2])-[#9,#17,#35,#53:3]",  # 2 matches
    "Semicarbazone": "[#1,#6]-[#6X3:1](-[#1,#6])=[#7X2:2]~[#7:3](-[#1,#6])-[#6X3](=[#8X1])-[#7X3](-[#1,#6])-[#1,#6]",  # 3 matches
    "Sulfuric Acid Diester": "[#8X1:1]=[#16X4:2](=[#8X1:3])(-[#8X2:4]-[#6])-[#8X2:5]-[#6]",  # 4 matches
    "Sulfuryl Halide": "[#8X1:1]=[#16X4:2](=[#8X1:3])(-[!#6&!#1:4])-[#9,#17,#35,#53]",  # 4 matches
    "Enediol": "[#1,#6]-[#6X3:1](-[#8X2]-[#1,#6])=[#6X3:2](-[#8X2]-[#1,#6])-[#1,#6]",  # 5 matches
    "Phosphoric Acid Halide": "[#15:1](=[#8X1:2])(-[#9,#17,#35,#53:3])(-[#8,#7,#9,#17,#35,#53])-[#8,#7,#9,#17,#35,#53]",  # 5 matches
    "Carboxylic Acid Amide Acetal": "[#6,#1]-[#6X4:1](-[#8X2:2]-[#6,#1])(-[#8X2:3]-[#6,#1])-[#7X3:4](-[#6,#1])-[#6,#1]",  # 6 matches
    "Carboxylic Acid Orthoester": "[#1,#6]-[#6X4:1](-[#8X2]-[#6])(-[#8X2]-[#6])-[#8X2]-[#6]",  # 7 matches
    "Sulfinic Acid Ester": "[#1,#6]-[#16X3:1](=[#8X1:2])-[#8X2:3]-[#6]",  # 7 matches
    "Sulfuric Acid": "[#8X1:1]=[#16X4:2](=[#8X1:3])(-[#8X2:4]-[!#6])-[#8X2:4]-[!#6]",  # 7 matches
    "Thiocarbonic Acid Diester": "[#6]-[#8X2:1]-[#6X3:2](=[#16X1:3])-[#8X2:4]-[#6]",  # 7 matches
    "Thioketone": "[#6X3:1](=[#16X1:2])(-[#6])-[#6]",  # 7 matches
    "Carbonic Acid Monoester": "[#6X3:1](=[#8X1:2])(-[#8X2:3]-[#1])-[#8X2:4]-[#6]",  # 8 matches
    "Semicarbazide": "[#1,#6]-[#7X3:1](-[#1,#6])-[#6X3:2](=[#8X1:3])-[#7X3:4](-[#1,#6])-[#7X3:5](-[#1,#6])-[#1,#6]",  # 8 matches
    "Sulfenic Acid": "[#6:1]-[#16X2:2]-[#8X2:3]-[#1:4]",  # 9 matches
    "Thiolactam": "[#6X3R:1](=[#16X1:2])-[#7X3R:3](-[#1,#6])-[R]",  # 9 matches
}

LOW_COVERAGE_PARAMETERS = [
    "b83",  # 0 matches
    "b49",  # 1 matches
    "b50",  # 1 matches
    "b80",  # 1 matches
    "b82",  # 2 matches
    "b79",  # 5 matches
    "b81",  # 10 matches
    "b55",  # 11 matches
    "b78",  # 14 matches
    "b29",  # 16 matches
    "b47",  # 16 matches
    "b54",  # 16 matches
    "b76",  # 16 matches
    "b44",  # 22 matches
    "b48",  # 22 matches
    "b23",  # 24 matches
    "b40",  # 24 matches
    "b63",  # 24 matches
    "b77",  # 24 matches
    "b33",  # 28 matches

    "a35",  # 0 matches
    "a36",  # 8 matches
    "a23",  # 26 matches

    "t31a",  # 7 matches
    "t101",  # 10 matches
    "t7",  # 12 matches
    "t8",  # 13 matches
    "t30",  # 16 matches
    "t100",  # 16 matches
    "t89",  # 18 matches
    "t126",  # 22 matches
    "t164",  # 24 matches



]

FORCEFIELD = """\
<?xml version="1.0" encoding="utf-8"?>
<SMIRNOFF version="0.3" aromaticity_model="OEAroModel_MDL">
    <Author>The Open Force Field Initiative</Author>
    <Date>2024-04-18</Date>
    <Constraints version="0.3">
        <Constraint smirks="[#1:1]-[*:2]" id="c1"></Constraint>
        <Constraint smirks="[#1:1]-[#8X2H2+0:2]-[#1]" id="c-tip3p-H-O" distance="0.9572 * angstrom ** 1"></Constraint>
        <Constraint smirks="[#1:1]-[#8X2H2+0]-[#1:2]" id="c-tip3p-H-O-H" distance="1.5139006545247014 * angstrom ** 1"></Constraint>
    </Constraints>
    <Bonds version="0.4" potential="harmonic" fractional_bondorder_method="AM1-Wiberg" fractional_bondorder_interpolation="linear">
        <Bond smirks="[#6X4:1]-[#6X4:2]" id="b1" length="1.533627603844 * angstrom ** 1" k="430.6027811279 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#6X3:2]" id="b2" length="1.50959128588 * angstrom ** 1" k="478.7391355181 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#6X3:2]=[#8X1+0]" id="b3" length="1.529016943769 * angstrom ** 1" k="405.2741855475 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#6X3:2]" id="b4" length="1.467873223618 * angstrom ** 1" k="534.8587647857 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]:[#6X3:2]" id="b5" length="1.400706064009 * angstrom ** 1" k="753.7514414653 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]=[#6X3:2]" id="b6" length="1.373209032276 * angstrom ** 1" k="904.0483623942 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]-[#7:2]" id="b7" length="1.477375101044 * angstrom ** 1" k="451.9422814679 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#7X3:2]" id="b8" length="1.389502949007 * angstrom ** 1" k="658.5261427735 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#7X3:2]-[#6X3]=[#8X1+0]" id="b9" length="1.462099794876 * angstrom ** 1" k="482.858522335 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1](=[#8X1+0])-[#7X3:2]" id="b10" length="1.383765741722 * angstrom ** 1" k="653.9549956682 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#7X2:2]" id="b11" length="1.386553566522 * angstrom ** 1" k="591.4003116832 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]:[#7X2,#7X3+1:2]" id="b12" length="1.338439274717 * angstrom ** 1" k="774.0798406678 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]=[#7X2,#7X3+1:2]" id="b13" length="1.309372378949 * angstrom ** 1" k="1010.327278232 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1](~!@[#7X3])(~!@[#7X3])~!@[#7X3:2]" id="b13a" length="1.304468222569 * angstrom ** 1" k="1171.510786135 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#6:1]-[#8:2]" id="b14" length="1.426323519316 * angstrom ** 1" k="517.9055609326 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#8X1-1:2]" id="b15" length="1.277730087271 * angstrom ** 1" k="1088.633667712 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#8X2H0:2]" id="b16" length="1.436323905911 * angstrom ** 1" k="454.0742933374 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#8X2:2]" id="b17" length="1.36337886368 * angstrom ** 1" k="587.6996587805 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#8X2H1:2]" id="b18" length="1.360361459598 * angstrom ** 1" k="692.682789202 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3a:1]-[#8X2H0:2]" id="b19" length="1.372026066276 * angstrom ** 1" k="637.5025487732 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1](=[#8X1])-[#8X2H0:2]" id="b20" length="1.356061963393 * angstrom ** 1" k="582.6841261163 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]=[#8X1+0,#8X2+1:2]" id="b21" length="1.225002296183 * angstrom ** 1" k="1524.077000087 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1](~[#8X1])~[#8X1:2]" id="b22" length="1.259829790192 * angstrom ** 1" k="1181.44544879 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]~[#8X2+1:2]~[#6X3]" id="b23" length="1.3571444823426182 * angstrom ** 1" k="609.7328918443294 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]-[#6:2]" id="b24" length="1.43086402681 * angstrom ** 1" k="659.9778648157 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]-[#6X4:2]" id="b25" length="1.46106500945 * angstrom ** 1" k="600.2001645155 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]=[#6X3:2]" id="b26" length="1.313760435783 * angstrom ** 1" k="1338.469879132 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]#[#7:2]" id="b27" length="1.166280484269 * angstrom ** 1" k="2661.430397009 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]#[#6X2:2]" id="b28" length="1.214299256799 * angstrom ** 1" k="2324.657925509 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]-[#8X2:2]" id="b29" length="1.3249753746973463 * angstrom ** 1" k="922.9469338550109 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]-[#7:2]" id="b30" length="1.336269158488 * angstrom ** 1" k="935.4250533384 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]=[#7:2]" id="b31" length="1.209557585349 * angstrom ** 1" k="1902.879533309 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]=[#6:2]" id="b32" length="1.670866007534 * angstrom ** 1" k="569.9324570507 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]=[#16:2]" id="b33" length="1.588783634816 * angstrom ** 1" k="864.5063028106 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#7:2]" id="b34" length="1.413406903546 * angstrom ** 1" k="573.1386995958 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7X3:1]-[#7X2:2]" id="b35" length="1.364566479126 * angstrom ** 1" k="588.0818493911 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7X2:1]-[#7X2:2]" id="b36" length="1.370103557277 * angstrom ** 1" k="512.4541632231 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]:[#7:2]" id="b37" length="1.336898000264 * angstrom ** 1" k="659.4003015214 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]=[#7:2]" id="b38" length="1.280744420247 * angstrom ** 1" k="1031.615802557 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7+1:1]=[#7-1:2]" id="b39" length="1.14427194841 * angstrom ** 1" k="2474.019968754 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]#[#7:2]" id="b40" length="1.111280919166 * angstrom ** 1" k="3236.575408342 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#8X2:2]" id="b41" length="1.412059933182 * angstrom ** 1" k="441.7395967793 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]~[#8X1:2]" id="b42" length="1.237741750517 * angstrom ** 1" k="1180.28701297 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#8X2:1]-[#8X2,#8X1-1:2]" id="b43" length="1.4548541788200349 * angstrom ** 1" k="428.0112402386294 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#6:2]" id="b44" length="1.807204863403 * angstrom ** 1" k="474.0210361996 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#16:1]-[#1:2]" id="b45" length="1.349293295059 * angstrom ** 1" k="588.817004054 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#16:2]" id="b46" length="2.098277726718 * angstrom ** 1" k="274.1014307954 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#9:2]" id="b47" length="1.6 * angstrom ** 1" k="750.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#16:1]-[#17:2]" id="b48" length="2.141076199528375 * angstrom ** 1" k="175.81141016193482 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#35:2]" id="b49" length="2.329705148471623 * angstrom ** 1" k="162.5090863354354 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#53:2]" id="b50" length="2.6 * angstrom ** 1" k="150.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#16X2,#16X1-1,#16X3+1:1]-[#6X4:2]" id="b51" length="1.836691225033 * angstrom ** 1" k="287.4704014027 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X2,#16X1-1,#16X3+1:1]-[#6X3:2]" id="b52" length="1.756884455253 * angstrom ** 1" k="353.7775371856 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X2,#16X1-1:1]-[#7:2]" id="b53" length="1.719854321742 * angstrom ** 1" k="186.0645726616 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X2:1]-[#8X2:2]" id="b54" length="1.668797225494 * angstrom ** 1" k="389.1109709639 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#16X2:1]=[#8X1,#7X2:2]" id="b55" length="1.5213240050039034 * angstrom ** 1" k="992.5556417122765 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X4,#16X3!+1:1]-[#6:2]" id="b56" length="1.812798632943 * angstrom ** 1" k="313.7423014234 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X4,#16X3:1]~[#7:2]" id="b57" length="1.717718078563 * angstrom ** 1" k="351.2725618136 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X4,#16X3:1]~[#7X2:2]" id="b57a" length="1.642019753341 * angstrom ** 1" k="479.6895747266 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X4,#16X3:1]-[#8X2:2]" id="b58" length="1.659151474326 * angstrom ** 1" k="397.9285337823 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16X4,#16X3:1]~[#8X1:2]" id="b59" length="1.469758533467 * angstrom ** 1" k="1216.071949922 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]-[#1:2]" id="b60" length="1.4089320994682575 * angstrom ** 1" k="499.55189102755895 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]~[#6:2]" id="b61" length="1.82693011113 * angstrom ** 1" k="347.2379459994 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]-[#7:2]" id="b62" length="1.661591255022 * angstrom ** 1" k="543.2245219816 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]=[#7:2]" id="b63" length="1.592080531463 * angstrom ** 1" k="733.4506557649 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]~[#8X2:2]" id="b64" length="1.628101720845 * angstrom ** 1" k="522.4590526951 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]~[#8X1:2]" id="b65" length="1.485495813453 * angstrom ** 1" k="1310.271428169 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#16:1]-[#15:2]" id="b66" length="2.137200875635 * angstrom ** 1" k="243.5522897142 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]=[#16X1:2]" id="b67" length="1.9335945395090406 * angstrom ** 1" k="542.1913362393467 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]-[#9:2]" id="b68" length="1.353861210984 * angstrom ** 1" k="710.5975770066 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#9:2]" id="b69" length="1.359315559786 * angstrom ** 1" k="575.8376418679 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]-[#17:2]" id="b70" length="1.749231486472 * angstrom ** 1" k="365.9497633523 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#17:2]" id="b71" length="1.802032150765 * angstrom ** 1" k="243.3914088645 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]-[#35:2]" id="b72" length="1.911620228289 * angstrom ** 1" k="301.0979455656 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X4:1]-[#35:2]" id="b73" length="1.975329839837 * angstrom ** 1" k="206.8714005796 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6:1]-[#53:2]" id="b74" length="2.198019945967 * angstrom ** 1" k="72.96409691036 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#6X4:1]-[#53:2]" id="b75" length="2.166 * angstrom ** 1" k="296.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#7:1]-[#9:2]" id="b76" length="1.45824047984 * angstrom ** 1" k="454.2090113047 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#17:2]" id="b77" length="1.788980256837101 * angstrom ** 1" k="294.5006529255279 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#35:2]" id="b78" length="1.8753894713826016 * angstrom ** 1" k="322.51897014080373 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#53:2]" id="b79" length="2.1 * angstrom ** 1" k="160.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#15:1]-[#9:2]" id="b80" length="1.64 * angstrom ** 1" k="880.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#15:1]-[#17:2]" id="b81" length="2.05892479375 * angstrom ** 1" k="283.81572655 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]-[#35:2]" id="b82" length="2.2727694770502263 * angstrom ** 1" k="232.77388221562418 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#15:1]-[#53:2]" id="b83" length="2.6 * angstrom ** 1" k="140.0 * angstrom ** -2 * mole ** -1 * kilocalorie ** 1"></Bond>
        <Bond smirks="[#6X4:1]-[#1:2]" id="b84" length="1.094206460752 * angstrom ** 1" k="715.6382948318 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X3:1]-[#1:2]" id="b85" length="1.08603610657 * angstrom ** 1" k="772.8798736582 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#6X2:1]-[#1:2]" id="b86" length="1.07097172423 * angstrom ** 1" k="932.0731929733 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#7:1]-[#1:2]" id="b87" length="1.018604730378 * angstrom ** 1" k="961.9809758788 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
        <Bond smirks="[#8:1]-[#1:2]" id="b88" length="0.9752301008088 * angstrom ** 1" k="1076.70158803 * kilocalorie_per_mole ** 1 * angstrom ** -2"></Bond>
    </Bonds>
    <Angles version="0.3" potential="harmonic">
        <Angle smirks="[*:1]~[#6X4:2]-[*:3]" angle="109.9380527584 * degree ** 1" k="133.5126366319 * kilocalorie_per_mole ** 1 * radian ** -2" id="a1"></Angle>
        <Angle smirks="[#1:1]-[#6X4:2]-[#1:3]" angle="108.3543135269 * degree ** 1" k="73.55327570151 * kilocalorie_per_mole ** 1 * radian ** -2" id="a2"></Angle>
        <Angle smirks="[*;r3:1]~;@[*;r3:2]~;!@[*:3]" angle="117.4648242289 * degree ** 1" k="116.6950409809 * kilocalorie_per_mole ** 1 * radian ** -2" id="a4"></Angle>
        <Angle smirks="[*:1]~;!@[*;r3:2]~;!@[*:3]" angle="114.9979324703 * degree ** 1" k="148.7002668567 * kilocalorie_per_mole ** 1 * radian ** -2" id="a5"></Angle>
        <Angle smirks="[#1:1]-[*;r3:2]~;!@[*:3]" angle="115.1109939866 * degree ** 1" k="55.50694049537 * kilocalorie_per_mole ** 1 * radian ** -2" id="a6"></Angle>
        <Angle smirks="[#6r4:1]-;@[#6r4:2]-;@[#6r4:3]" angle="87.42984452369 * degree ** 1" k="141.9340831094 * kilocalorie_per_mole ** 1 * radian ** -2" id="a7"></Angle>
        <Angle smirks="[!#1:1]-[#6r4:2]-;!@[!#1:3]" angle="115.1602720493 * degree ** 1" k="212.5869538277 * kilocalorie_per_mole ** 1 * radian ** -2" id="a8"></Angle>
        <Angle smirks="[!#1:1]-[#6r4:2]-;!@[#1:3]" angle="113.7820142404 * degree ** 1" k="115.5412584893 * kilocalorie_per_mole ** 1 * radian ** -2" id="a9"></Angle>
        <Angle smirks="[*:1]~[#6X3:2]~[*:3]" angle="120.1049996386 * degree ** 1" k="169.0953155969 * kilocalorie_per_mole ** 1 * radian ** -2" id="a10"></Angle>
        <Angle smirks="[#1:1]-[#6X3:2]~[*:3]" angle="119.8544859917 * degree ** 1" k="65.84475468128 * kilocalorie_per_mole ** 1 * radian ** -2" id="a11"></Angle>
        <Angle smirks="[#1:1]-[#6X3:2]-[#1:3]" angle="117.693379803 * degree ** 1" k="43.36068465475 * kilocalorie_per_mole ** 1 * radian ** -2" id="a12"></Angle>
        <Angle smirks="[*;r6:1]~;@[*;r5:2]~;@[*;r5;x2:3]" angle="129.7106976744 * degree ** 1" k="153.6717495861 * kilocalorie_per_mole ** 1 * radian ** -2" id="a13"></Angle>
        <Angle smirks="[*;r6:1]~;@[*;r5;x4,*;r5;X4:2]~;@[*;r5;x2:3]" angle="110.8145273566 * degree ** 1" k="224.9724108845 * kilocalorie_per_mole ** 1 * radian ** -2" id="a13a"></Angle>
        <Angle smirks="[*:1]~;!@[*;X3;r5:2]~;@[*;r5:3]" angle="124.801611398 * degree ** 1" k="101.803524732 * kilocalorie_per_mole ** 1 * radian ** -2" id="a14"></Angle>
        <Angle smirks="[#8X1:1]~[#6X3:2]~[#8:3]" angle="123.6058900035 * degree ** 1" k="126.6200669144 * kilocalorie_per_mole ** 1 * radian ** -2" id="a15"></Angle>
        <Angle smirks="[*:1]~[#6X2:2]~[*:3]" angle="178.06592484092022 * degree ** 1" k="99.91851476849 * kilocalorie_per_mole ** 1 * radian ** -2" id="a16"></Angle>
        <Angle smirks="[*:1]~[#7X2:2]~[*:3]" angle="176.0234545467476 * degree ** 1" k="89.96041175564783 * kilocalorie_per_mole ** 1 * radian ** -2" id="a17"></Angle>
        <Angle smirks="[*:1]~[#7X4,#7X3,#7X2-1:2]~[*:3]" angle="113.2116558678 * degree ** 1" k="248.1768682614 * kilocalorie_per_mole ** 1 * radian ** -2" id="a18"></Angle>
        <Angle smirks="[*:1]@-[r!r6;#7X4,#7X3,#7X2-1:2]@-[*:3]" angle="93.92727646489 * degree ** 1" k="187.8484236925 * kilocalorie_per_mole ** 1 * radian ** -2" id="a18a"></Angle>
        <Angle smirks="[#1:1]-[#7X4,#7X3,#7X2-1:2]-[*:3]" angle="109.2513757493 * degree ** 1" k="107.6482194711 * kilocalorie_per_mole ** 1 * radian ** -2" id="a19"></Angle>
        <Angle smirks="[*:1]~[#7X3$(*~[#6X3,#6X2,#7X2+0]):2]~[*:3]" angle="121.7705343867 * degree ** 1" k="158.9566366884 * kilocalorie_per_mole ** 1 * radian ** -2" id="a20"></Angle>
        <Angle smirks="[#1:1]-[#7X3$(*~[#6X3,#6X2,#7X2+0]):2]-[*:3]" angle="118.0636611293 * degree ** 1" k="74.20968211314 * kilocalorie_per_mole ** 1 * radian ** -2" id="a21"></Angle>
        <Angle smirks="[*:1]~[#7X2+0:2]~[*:3]" angle="117.9267269648 * degree ** 1" k="290.5379151297 * kilocalorie_per_mole ** 1 * radian ** -2" id="a22"></Angle>
        <Angle smirks="[*:1]~[#7X2+0:2]~[#6X2:3](~[#16X1])" angle="144.3131862361 * degree ** 1" k="150.3988349754 * kilocalorie_per_mole ** 1 * radian ** -2" id="a23"></Angle>
        <Angle smirks="[#1:1]-[#7X2+0:2]~[*:3]" angle="111.6159575824 * degree ** 1" k="104.6951323738 * kilocalorie_per_mole ** 1 * radian ** -2" id="a24"></Angle>
        <Angle smirks="[#6,#7,#8:1]-[#7X3:2](~[#8X1])~[#8X1:3]" angle="117.5701099391 * degree ** 1" k="145.0320372744 * kilocalorie_per_mole ** 1 * radian ** -2" id="a25"></Angle>
        <Angle smirks="[#8X1:1]~[#7X3:2]~[#8X1:3]" angle="124.4551761629 * degree ** 1" k="140.3499034379 * kilocalorie_per_mole ** 1 * radian ** -2" id="a26"></Angle>
        <Angle smirks="[*:1]~[#7X2:2]~[#7X1:3]" angle="175.22046551603754 * degree ** 1" k="117.5667280814 * kilocalorie_per_mole ** 1 * radian ** -2" id="a27"></Angle>
        <Angle smirks="[*:1]-[#8:2]-[*:3]" angle="112.8736161145 * degree ** 1" k="239.791779107 * kilocalorie_per_mole ** 1 * radian ** -2" id="a28"></Angle>
        <Angle smirks="[#6X3,#7:1]~;@[#8;r:2]~;@[#6X3,#7:3]" angle="120.5522025675 * degree ** 1" k="298.0648096221 * kilocalorie_per_mole ** 1 * radian ** -2" id="a29"></Angle>
        <Angle smirks="[*:1]-[#8X2+1:2]=[*:3]" angle="122.99227453817346 * degree ** 1" k="323.7005670962272 * kilocalorie_per_mole ** 1 * radian ** -2" id="a30"></Angle>
        <Angle smirks="[*:1]~[#16X4:2]~[*:3]" angle="120.4494266916 * degree ** 1" k="188.502182053 * kilocalorie_per_mole ** 1 * radian ** -2" id="a31"></Angle>
        <Angle smirks="[*:1]-[#16X4,#16X3+0:2]~[*:3]" angle="107.2128707706 * degree ** 1" k="239.4251194442 * kilocalorie_per_mole ** 1 * radian ** -2" id="a32"></Angle>
        <Angle smirks="[*:1]~[#16X3$(*~[#8X1,#7X2]):2]~[*:3]" angle="102.1939451918 * degree ** 1" k="261.1995820554 * kilocalorie_per_mole ** 1 * radian ** -2" id="a33"></Angle>
        <Angle smirks="[*:1]~[#16X2,#16X3+1:2]~[*:3]" angle="100.2622716311 * degree ** 1" k="300.4291321431 * kilocalorie_per_mole ** 1 * radian ** -2" id="a34"></Angle>
        <Angle smirks="[*:1]=[#16X2:2]=[*:3]" angle="180.0 * degree ** 1" k="140.0 * mole ** -1 * radian ** -2 * kilocalorie ** 1" id="a35"></Angle>
        <Angle smirks="[*:1]=[#16X2:2]=[#8:3]" angle="112.6164646730111 * degree ** 1" k="259.32893235640387 * kilocalorie_per_mole ** 1 * radian ** -2" id="a36"></Angle>
        <Angle smirks="[#6X3:1]-[#16X2:2]-[#6X3:3]" angle="101.7625315251 * degree ** 1" k="301.8153447267 * kilocalorie_per_mole ** 1 * radian ** -2" id="a37"></Angle>
        <Angle smirks="[#6X3:1]-[#16X2:2]-[#6X4:3]" angle="101.8447149778 * degree ** 1" k="350.5323968615 * kilocalorie_per_mole ** 1 * radian ** -2" id="a38"></Angle>
        <Angle smirks="[#6X3:1]-[#16X2:2]-[#1:3]" angle="94.63963344093 * degree ** 1" k="170.9506636668 * kilocalorie_per_mole ** 1 * radian ** -2" id="a39"></Angle>
        <Angle smirks="[*:1]~[#15:2]~[*:3]" angle="108.9071707171 * degree ** 1" k="179.7146443371 * kilocalorie_per_mole ** 1 * radian ** -2" id="a40"></Angle>
        <Angle smirks="[*;r5:1]1@[*;r5:2]@[*;r5:3]@[*;r5]@[*;r5]1" angle="108.1730591511 * degree ** 1" k="183.0371178083 * kilocalorie_per_mole ** 1 * radian ** -2" id="a41"></Angle>
        <Angle smirks="[*;r5:1]1@[#16;r5:2]@[*;r5:3]@[*;r5]@[*;r5]1" angle="90.5472249729 * degree ** 1" k="250.6788029697 * kilocalorie_per_mole ** 1 * radian ** -2" id="a41a"></Angle>
        <Angle smirks="[*;r3:1]1~;@[*;r3:2]~;@[*;r3:3]1" angle="60.00126695381 * degree ** 1" k="111.1759590839 * kilocalorie_per_mole ** 1 * radian ** -2" id="a3"></Angle>
    </Angles>
    <ProperTorsions version="0.4" potential="k*(1+cos(periodicity*theta-phase))" default_idivf="auto" fractional_bondorder_method="AM1-Wiberg" fractional_bondorder_interpolation="linear">
        <Proper smirks="[*:1]-[#6X4:2]-[#6X4:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t1" k1="0.143904881748 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#6X4:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t2" k1="0.3549667638131 * mole ** -1 * kilocalorie ** 1" k2="0.2399307824079 * mole ** -1 * kilocalorie ** 1" k3="0.9156101580834 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#1:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t3" k1="0.2279946533057 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#6X4:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t4" k1="0.0985377828486 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#8X2:1]-[#6X4:2]-[#6X4:3]-[#8X2:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t5" k1="-0.01533770820035 * mole ** -1 * kilocalorie ** 1" k2="0.4191503124266 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#9:1]-[#6X4:2]-[#6X4:3]-[#9:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t6" k1="0.07331659440315 * mole ** -1 * kilocalorie ** 1" k2="-0.1968789511389 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#17:1]-[#6X4:2]-[#6X4:3]-[#17:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t7" k1="0.6394408470579 * mole ** -1 * kilocalorie ** 1" k2="-1.404270761131 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#35:1]-[#6X4:2]-[#6X4:3]-[#35:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t8" k1="1.077770345087 * mole ** -1 * kilocalorie ** 1" k2="-0.1123758770255 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#8X2:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t9" k1="0.1311347012225 * mole ** -1 * kilocalorie ** 1" k2="0.436546671918 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#9:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t10" k1="0.09618308954761 * mole ** -1 * kilocalorie ** 1" k2="0.4297431625272 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#17:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t11" k1="0.2304293564097 * mole ** -1 * kilocalorie ** 1" k2="0.7025266140706 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X4:3]-[#35:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t12" k1="0.1338552866344 * mole ** -1 * kilocalorie ** 1" k2="0.6076851605113 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#6X4;r3:3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t13" k1="1.736222827898 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#6X4;r3:3]-[#6X4;r3:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t14" k1="0.4161025723994 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4;r3:2]-@[#6X4;r3:3]-[*:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t15" k1="-2.743657257903 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-[#6X4;r3:2]-[#6X4;r3:3]-[*:4]" periodicity1="2" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t16" k1="-0.6662164025773 * mole ** -1 * kilocalorie ** 1" k2="4.596360017857 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]-[#6X4:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t17" k1="0.1898990064757 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#6X3:3]=[*:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t18" k1="-0.4476383757771 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#6X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t18a" k1="-0.2045307565273 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#6X3:3](~!@[#7X3])~!@[#7X3:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t18b" k1="-0.1892404337724 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X3:3]=[#8X1:4]" periodicity1="1" periodicity2="2" periodicity3="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t19" k1="0.9496608306703 * mole ** -1 * kilocalorie ** 1" k2="0.2278538631676 * mole ** -1 * kilocalorie ** 1" k3="-0.1853374737232 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X3:3](~[#8X1])~[#8X1:4]" periodicity1="1" periodicity2="2" periodicity3="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t19a" k1="-0.05752542044084 * mole ** -1 * kilocalorie ** 1" k2="0.3423737582396 * mole ** -1 * kilocalorie ** 1" k3="0.2544090180879 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#6X3:3]=[#6X3:4]" periodicity1="3" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t20" k1="0.2774265594871 * mole ** -1 * kilocalorie ** 1" k2="0.1636595455502 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#6X4:2]-[#6X3:3]=[#6X3:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t21" k1="-0.1380115056627 * mole ** -1 * kilocalorie ** 1" k2="0.5548313330339 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#7X3:1]-[#6X4:2]-[#6X3:3]-[#7X3:4]" periodicity1="1" periodicity2="2" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t22" k1="-0.4849819360398 * mole ** -1 * kilocalorie ** 1" k2="0.48422835545 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#6X3:3]-[#7X3:4]" periodicity1="4" periodicity2="2" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t23" k1="-0.2974567669267 * mole ** -1 * kilocalorie ** 1" k2="0.3173126044464 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#16X2,#16X1-1,#16X3+1:1]-[#6X3:2]-[#6X4:3]-[#1:4]" periodicity1="2" periodicity2="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t24" k1="-0.5853904140938 * mole ** -1 * kilocalorie ** 1" k2="-0.3225535130855 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#16X2,#16X1-1,#16X3+1:1]-[#6X3:2]-[#6X4:3]-[#7X4,#7X3:4]" periodicity1="4" periodicity2="3" periodicity3="2" periodicity4="2" periodicity5="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" phase4="270.0 * degree ** 1" phase5="90.0 * degree ** 1" id="t25" k1="-0.3677812372708 * mole ** -1 * kilocalorie ** 1" k2="-0.1960875386973 * mole ** -1 * kilocalorie ** 1" k3="-0.7987811391557 * mole ** -1 * kilocalorie ** 1" k4="0.02719826654294 * mole ** -1 * kilocalorie ** 1" k5="0.07104237971609 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0" idivf4="1.0" idivf5="1.0"></Proper>
        <Proper smirks="[#16X2,#16X1-1,#16X3+1:1]-[#6X3:2]-[#6X4:3]-[#7X3$(*-[#6X3,#6X2]):4]" periodicity1="4" periodicity2="3" periodicity3="2" periodicity4="2" periodicity5="1" periodicity6="1" phase1="270.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" phase4="270.0 * degree ** 1" phase5="270.0 * degree ** 1" phase6="0.0 * degree ** 1" id="t26" k1="0.161707497114 * mole ** -1 * kilocalorie ** 1" k2="-0.01667434144208 * mole ** -1 * kilocalorie ** 1" k3="0.784503538146 * mole ** -1 * kilocalorie ** 1" k4="-0.1211229377652 * mole ** -1 * kilocalorie ** 1" k5="-0.09240807805986 * mole ** -1 * kilocalorie ** 1" k6="-0.5292154420027 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0" idivf4="1.0" idivf5="1.0" idivf6="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4;r3:2]-[#6X3:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t27" k1="0.4315814539879 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4;r3:2]-[#6X3:3]~[#6X3:4]" periodicity1="4" periodicity2="2" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t28" k1="0.4800077348792 * mole ** -1 * kilocalorie ** 1" k2="0.2159518662244 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4;r3:2]-[#6X3:3]~[#6X3:4]" periodicity1="4" periodicity2="3" periodicity3="2" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t29" k1="0.01252436602981 * mole ** -1 * kilocalorie ** 1" k2="0.3272620180813 * mole ** -1 * kilocalorie ** 1" k3="-0.1681548719502 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#6X4;r3:2]-[#6X3:3]-[#7X3:4]" periodicity1="2" periodicity2="1" periodicity3="3" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t30" k1="-0.4094047131311 * mole ** -1 * kilocalorie ** 1" k2="0.7214198085497 * mole ** -1 * kilocalorie ** 1" k3="-0.4213069561783 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#6X4;r3:2]-[#6X3:3]=[#8X1:4]" periodicity1="2" periodicity2="1" periodicity3="3" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t31" k1="1.193647802236 * mole ** -1 * kilocalorie ** 1" k2="-0.6244241299401 * mole ** -1 * kilocalorie ** 1" k3="0.07846983414274 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#6X4;r3:2]-[#6X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" periodicity2="1" periodicity3="3" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t31a" k1="1.748610407917 * mole ** -1 * kilocalorie ** 1" k2="-0.2239172252564 * mole ** -1 * kilocalorie ** 1" k3="-0.0834281217792 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#6X4;r3:2]-[#6X3:3]~[#6X3:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t32" k1="-0.395709109761 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#7X3:1]-[#6X4;r3:2]-[#6X3:3]~[#6X3:4]" periodicity1="4" periodicity2="2" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t33" k1="0.05404426351766 * mole ** -1 * kilocalorie ** 1" k2="-0.3514574174509 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3]~[#6X3:4]" periodicity1="4" periodicity2="3" periodicity3="2" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t34" k1="0.2734748485887 * mole ** -1 * kilocalorie ** 1" k2="-2.70750977848 * mole ** -1 * kilocalorie ** 1" k3="2.361016794234 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3;r6:3]:[#6X3;r6:4]" periodicity1="4" periodicity2="2" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t35" k1="0.02771085451639 * mole ** -1 * kilocalorie ** 1" k2="1.488097446031 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3;r5:3]-;@[#6X3;r5:4]" periodicity1="4" periodicity2="3" periodicity3="2" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t36" k1="-0.08364617973494 * mole ** -1 * kilocalorie ** 1" k2="0.1280947086012 * mole ** -1 * kilocalorie ** 1" k3="2.346418926187 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3;r5:3]=;@[#6X3;r5:4]" periodicity1="1" phase1="180.0 * degree ** 1" id="t37" k1="-0.3119839227655 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3]-[#6X4:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t38" k1="0.3978561533395 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3;r6:3]:[#7X2;r6:4]" periodicity1="2" periodicity2="1" periodicity3="3" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t39" k1="2.111316901374 * mole ** -1 * kilocalorie ** 1" k2="-0.4828841788244 * mole ** -1 * kilocalorie ** 1" k3="1.319583513824 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3]=[#7X2:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t40" k1="-0.878710998028 * mole ** -1 * kilocalorie ** 1" k2="1.360033073618 * mole ** -1 * kilocalorie ** 1" k3="-0.1170098528755 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3]-[#8X2:4]" periodicity1="4" periodicity2="2" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t41" k1="-0.1114930646196 * mole ** -1 * kilocalorie ** 1" k2="2.466934789683 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3]=[#8X1:4]" periodicity1="2" phase1="320.0 * degree ** 1" id="t42" k1="-0.9759476588046 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4;r3:1]-;@[#6X4;r3:2]-[#6X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" phase1="320.0 * degree ** 1" id="t42a" k1="-0.4846378709475 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]-[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t43" k1="1.237505194022 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]:[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t44" k1="3.268474846449 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-,:[#6X3:2]=[#6X3:3]-,:[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t45" k1="4.626306460904 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X3:2]=[#6X3:3]-[#6X4:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t46" k1="3.154324713915 * mole ** -1 * kilocalorie ** 1" k2="-0.4171138742327 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]-[#6X3$(*=[#8,#16,#7]):3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t47" k1="1.042756265598 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]=[#6X3:2]-[#6X3:3]=[#8X1:4]" periodicity1="2" periodicity2="3" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t48" k1="1.063790001652 * mole ** -1 * kilocalorie ** 1" k2="0.6201579981885 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]=[#6X3:2]-[#6X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" periodicity2="3" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t48a" k1="-0.02403160784339 * mole ** -1 * kilocalorie ** 1" k2="-1.083518405649 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]~[#7a:2]:[#6a:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t49" k1="4.305064155732 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X4:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t50" k1="0.1113575199827 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X4:2]-[#7X3:3]~[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t51" k1="0.2867984266596 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X3:3]-[#7X2:4]=[#6]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t52" k1="0.6470596872399 * mole ** -1 * kilocalorie ** 1" k2="-0.1983730964578 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#7X3:3]-[#7X2:4]=[#6]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t53" k1="0.3818665742616 * mole ** -1 * kilocalorie ** 1" k2="-0.17410932304 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X3:3]-[#7X2:4]=[#7X2,#8X1]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t54" k1="-0.4848481183556 * mole ** -1 * kilocalorie ** 1" k2="3.726328089482 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#7X3:3]-[#7X2:4]=[#7X2,#8X1]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t55" k1="0.2512425443041 * mole ** -1 * kilocalorie ** 1" k2="4.599675322718 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X3$(*@1-[*]=,:[*][*]=,:[*]@1):3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t56" k1="0.5352483896472 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#6X4:2]-[#7X3$(*@1-[*]=,:[*][*]=,:[*]@1):3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t57" k1="0.840277035679 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#7X4,#7X3:3]-[#6X4:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t58" k1="0.07484583104594 * mole ** -1 * kilocalorie ** 1" k2="0.3153839995235 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#7X4,#7X3:2]-[#6X4;r3:3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t59" k1="-1.360848751627 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#7X4,#7X3:2]-[#6X4;r3:3]-[#6X4;r3:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t60" k1="-0.09475244075082 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[!#1:1]-[#7X4,#7X3:2]-[#6X4;r3:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t61" k1="0.2766628958548 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[!#1:1]-[#7X4,#7X3:2]-[#6X4;r3:3]-[#6X4;r3:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t62" k1="1.265478740542 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X4:2]-[#6X3:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t63" k1="-0.003550541253985 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X3$(*~[#6X3,#6X2]):3]~[*:4]" periodicity1="2" periodicity2="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t64" k1="0.3215160932459 * mole ** -1 * kilocalorie ** 1" k2="0.2510071310501 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#7X3:3](~[#8X1])~[#8X1:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t65" k1="0.06888486671287 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#7X3:2]-[#6X4:3]-[#6X3:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t66" k1="-0.5801351028897 * mole ** -1 * kilocalorie ** 1" k2="-0.5728076214143 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#7X3:3]-[#6X3:4]=[#8,#16,#7]" periodicity1="4" periodicity2="3" periodicity3="2" periodicity4="1" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="0.0 * degree ** 1" phase4="0.0 * degree ** 1" id="t67" k1="-0.1108037280545 * mole ** -1 * kilocalorie ** 1" k2="-0.2219080374426 * mole ** -1 * kilocalorie ** 1" k3="0.8434650381516 * mole ** -1 * kilocalorie ** 1" k4="-0.1523281600983 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0" idivf4="1.0"></Proper>
        <Proper smirks="[#8X2H0:1]-[#6X4:2]-[#7X3:3]-[#6X3:4]" periodicity1="2" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t68" k1="1.893548769818 * mole ** -1 * kilocalorie ** 1" k2="-0.6737076865819 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#7X3:2]-[#6X4;r3:3]-[#6X4;r3:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t69" k1="0.5428784363215 * mole ** -1 * kilocalorie ** 1" k2="-1.059754067776 * mole ** -1 * kilocalorie ** 1" k3="-1.086161039623 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X2:2]-[#6X4:3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t70" k1="-0.5218250375353 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]=[#7X2,#7X3+1:2]-[#6X4:3]-[#1:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t71" k1="1.601769915929 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]=[#7X2,#7X3+1:2]-[#6X4:3]-[#6X3,#6X4:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t72" k1="1.105872590367 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X3,#7X2-1:2]-[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t73" k1="0.7020804385738 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X3,#7X2-1:2]-!@[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t74" k1="1.347987439355 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3:2]-[#6X3$(*=[#8,#16,#7]):3]~[*:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t75" k1="2.130520404521 * mole ** -1 * kilocalorie ** 1" k2="0.294037904962 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#7X3:2]-[#6X3:3]=[#8,#16,#7:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t76" k1="0.6152764778624 * mole ** -1 * kilocalorie ** 1" k2="1.28650661597 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3:2]-!@[#6X3:3](=[#8,#16,#7:4])-[#6,#1]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t77" k1="2.471212234633 * mole ** -1 * kilocalorie ** 1" k2="-0.5572975556525 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#1:1]-[#7X3:2]-!@[#6X3:3](=[#8,#16,#7:4])-[#6,#1]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t78" k1="1.269440807926 * mole ** -1 * kilocalorie ** 1" k2="1.252154007785 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3:2]-!@[#6X3:3](=[#8,#16,#7:4])-[#7X3]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t79" k1="0.9716617682348 * mole ** -1 * kilocalorie ** 1" k2="0.7987762375066 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3;r5:2]-@[#6X3;r5:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t80" k1="1.469128273675 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#7X3:2]~[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t81" k1="0.4873119328207 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]=[#7X2,#7X3+1:2]-[#6X3:3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t82" k1="1.079567853188 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X3:2]-[#7X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t82a" k1="0.5938751336386 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]=[#7X2,#7X3+1:2]-[#6X3:3]=,:[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t83" k1="1.409955177277 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]=,:[#6X3:2]-[#7X3:3](~[#8X1])~[#8X1:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t83a" k1="1.159949773741 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X2,#7X3$(*~[#8X1]):2]:[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t84" k1="2.22025750296 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]:[#7X2:2]:[#6X3:3]:[#6X3:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t85" k1="5.590150698229 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-,:[#6X3:2]=[#7X2:3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t86" k1="8.052532732388 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3+1:2]=,:[#6X3:3]-,:[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t87" k1="1.012490953194 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3:2]~!@[#6X3:3](~!@[#7X3])~!@[#7X3:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t87a" k1="0.809728596164 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#16X4,#16X3+0:1]-[#7X2:2]=[#6X3:3]-[#7X3:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t88" k1="3.120029548599 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#16X4,#16X3+0:1]-[#7X2:2]=[#6X3:3]-[#16X2,#16X3+1:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t89" k1="3.602517565373 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#7X2:1]~[#7X2:2]-[#6X3:3]~[#6X3:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t90" k1="2.01411152171 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#7X2:1]~[#7X2:2]-[#6X4:3]-[#6X3:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t91" k1="0.4553303737682 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#7X2:1]~[#7X2:2]-[#6X4:3]~[#1:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t92" k1="0.383152688968 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#8X2:3]-[#1:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t93" k1="1.090880965393 * mole ** -1 * kilocalorie ** 1" idivf1="3.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#8X2H1:3]-[#1:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t94" k1="0.4431584714139 * mole ** -1 * kilocalorie ** 1" k2="0.1140296769156 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#6X4:2]-[#8X2H0:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t95" k1="0.7893340970024 * mole ** -1 * kilocalorie ** 1" idivf1="3.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#8X2H0:3]-[#6X4:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t96" k1="0.3102287948961 * mole ** -1 * kilocalorie ** 1" k2="-0.04857068407726 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#6X4:2]-[#8X2:3]-[#6X3:4]" periodicity1="3" periodicity2="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t97" k1="0.111302960584 * mole ** -1 * kilocalorie ** 1" k2="0.438911435509 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#8X2:2]-[#6X4:3]-[#8X2:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="180.0 * degree ** 1" id="t98" k1="0.1264272535848 * mole ** -1 * kilocalorie ** 1" k2="0.6515718070007 * mole ** -1 * kilocalorie ** 1" k3="1.46947387164 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#8X2:2]-[#6X4:3]-[#7X3:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t99" k1="-0.0904570525121 * mole ** -1 * kilocalorie ** 1" k2="0.4589116214921 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#8X2:2]-[#6X4;r3:3]-@[#6X4;r3:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t100" k1="-1.803329655416 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#8X2:2]-[#6X4;r3:3]-[#1:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t101" k1="-0.6734456899116 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#8X2:2]-[#6X4;r3:3]-[#1:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t102" k1="0.4568884103229 * mole ** -1 * kilocalorie ** 1" k2="0.05646418429228 * mole ** -1 * kilocalorie ** 1" k3="-0.2753920730159 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#1:1]-[#8X2:2]-[#6X4;r3:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t103" k1="0.3752333067098 * mole ** -1 * kilocalorie ** 1" k2="0.113484739058 * mole ** -1 * kilocalorie ** 1" k3="-0.4478184326725 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#1:1]-[#8X2:2]-[#6X4;r3:3]-[#6X4;r3:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t104" k1="0.5403812598362 * mole ** -1 * kilocalorie ** 1" k2="0.7070980186907 * mole ** -1 * kilocalorie ** 1" k3="-0.01860510796078 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]-[#8X2:3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t105" k1="1.579560403188 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2]-[#8X2:3]-[#1:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t106" k1="0.9816061886151 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2](=[#8,#16,#7])-[#8X2H0:3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t107" k1="3.481871717726 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#6X3:2](=[#8,#16,#7])-[#8:3]-[#1:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t108" k1="3.228687140458 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#8X2:2]-[#6X3:3]=[#8X1:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t109" k1="2.728514939916 * mole ** -1 * kilocalorie ** 1" k2="-0.2193913975874 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#8,#16,#7:1]=[#6X3:2]-[#8X2H0:3]-[#6X4:4]" periodicity1="2" periodicity2="1" phase1="180.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t110" k1="0.3021951447071 * mole ** -1 * kilocalorie ** 1" k2="1.350045656671 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#8X2:2]@[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t111" k1="1.763607254567 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#8X2+1:2]=[#6X3:3]-[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t112" k1="7.917885966353 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]=[#8X2+1:2]-[#6:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t113" k1="0.6524097319567 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#16:2]=,:[#6:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t114" k1="-1.975848663415 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#16X2,#16X3+1:2]-[#6:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t115" k1="0.4059773313175 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#16X2,#16X3+1:2]-[#6:3]-[#1:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t116" k1="0.4191159286634 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]-@[#16X2,#16X1-1,#16X3+1:2]-@[#6X3,#7X2;r5:3]=@[#6,#7;r5:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t117" k1="8.980846475502 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#16X4,#16X3!+1:2]-[#6X4:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t118" k1="0.2057583936885 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#6X4:3]-[#1:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t119" k1="-0.5706862605235 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#6X4:3]~[#6X4:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t120" k1="-0.1566858674916 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#16X4,#16X3+0:2]-[#6X3:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t121" k1="0.4470753837741 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6:1]-[#16X4,#16X3+0:2]-[#6X3:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t122" k1="0.5793286462619 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#15:2]-[#6X4:3]-[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t123a" k1="0.1097523554481 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#15:2]-[#6X3:3]~[*:4]" periodicity1="2" periodicity2="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t124" k1="-2.188848684957 * mole ** -1 * kilocalorie ** 1" k2="0.267367428569 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#8:2]-[#8:3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t125" k1="2.015655556333 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#8:2]-[#8H1:3]-[*:4]" periodicity1="2" phase1="0.0 * degree ** 1" id="t126" k1="0.9161192279719 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#8X2:2]-[#7:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t127" k1="1.95459294494 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#8X2r5:2]-;@[#7X3r5:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t128" k1="1.122808273354 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#8X2r5:2]-;@[#7X2r5:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t129" k1="-19.9078720572 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X4,#7X3:2]-[#7X4,#7X3:3]~[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t130" k1="0.8225345259366 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#1:1]-[#7X4,#7X3:2]-[#7X4,#7X3:3]-[#1:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t131" k1="0.07410908097399 * mole ** -1 * kilocalorie ** 1" k2="0.6527078439557 * mole ** -1 * kilocalorie ** 1" k3="0.4247650388113 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#7X4,#7X3:2]-[#7X4,#7X3:3]-[#1:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t132" k1="0.3837081606485 * mole ** -1 * kilocalorie ** 1" k2="0.3323343731487 * mole ** -1 * kilocalorie ** 1" k3="0.6246706443245 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#7X4,#7X3:2]-[#7X4,#7X3:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t133" k1="-0.002349476678131 * mole ** -1 * kilocalorie ** 1" k2="0.3171660676828 * mole ** -1 * kilocalorie ** 1" k3="0.6774831253141 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X4,#7X3:2]-[#7X3$(*~[#6X3,#6X2]):3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t134" k1="-1.10501888253 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3$(*-[#6X3,#6X2]):2]-[#7X3$(*-[#6X3,#6X2]):3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t135" k1="-0.6719845598606 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7X3$(*-[#6X3,#6X2])r5:2]-@[#7X3$(*-[#6X3,#6X2])r5:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t136" k1="-0.4959042611063 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]@[#7X2:2]@[#7X2:3]@[#7X2,#6X3:4]" periodicity1="1" phase1="180.0 * degree ** 1" id="t137" k1="4.605325210702 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X2:2]-[#7X3:3]~[*:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t138" k1="-0.4769457729479 * mole ** -1 * kilocalorie ** 1" k2="2.055608777607 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X2:2]-[#7X4:3]~[*:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t138a" k1="-0.09510851427019 * mole ** -1 * kilocalorie ** 1" k2="1.554435554256 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]=[#7X2:2]-[#7X2:3]=[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t139" k1="4.289753656488 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X2:2]=,:[#7X2:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t140" k1="15.78931647465 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7X3+1:2]=,:[#7X2:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t141" k1="10.57958597957 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7x3:2]-[#7x3,#6x3:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t141a" k1="-3.906902709944 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7x2:2]-[#7x3:3]~[*:4]" periodicity1="3" periodicity2="2" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" id="t141b" k1="1.114119889873 * mole ** -1 * kilocalorie ** 1" k2="0.5022068651646 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]~[#6x3:2](~[#7,#8,#16])-[#6x3:3]~[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t141c" k1="-3.525605054758 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#16X2,#16X3+1:2]-[!#6:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t142" k1="-0.7577046066374 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#16X4,#16X3+0:2]-[#7:3]~[*:4]" periodicity1="1" periodicity2="2" periodicity3="3" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t143" k1="-1.572239594429 * mole ** -1 * kilocalorie ** 1" k2="0.316444679891 * mole ** -1 * kilocalorie ** 1" k3="0.1426731536269 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#1:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t144" k1="-0.4084397768951 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#1:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t145" k1="0.1933957147707 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#6X4:4]" periodicity1="1" periodicity2="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t146" k1="0.6326430639456 * mole ** -1 * kilocalorie ** 1" k2="0.3779606752882 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t147" k1="0.8578473753009 * mole ** -1 * kilocalorie ** 1" k2="0.5104124634454 * mole ** -1 * kilocalorie ** 1" k3="1.26988246352 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#1:4]" periodicity1="1" periodicity2="3" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t148" k1="-0.6764056804828 * mole ** -1 * kilocalorie ** 1" k2="0.286385900666 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#16X4,#16X3+0:2]-[#7X4,#7X3:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="180.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t149" k1="-0.01925679287597 * mole ** -1 * kilocalorie ** 1" k2="0.5508778009737 * mole ** -1 * kilocalorie ** 1" k3="1.543436668678 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#16X4,#16X3+0:2]-[#7X3:3]-[#6X3:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t150" k1="0.4603759126772 * mole ** -1 * kilocalorie ** 1" k2="0.587206663536 * mole ** -1 * kilocalorie ** 1" k3="-0.5000049198338 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#7X3:3]-[#6X3:4]" periodicity1="3" periodicity2="2" phase1="90.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t151" k1="-0.5704900776124 * mole ** -1 * kilocalorie ** 1" k2="1.264269121276 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#16X4,#16X3+0:2]-[#7X3:3]-[#6X3:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t152" k1="-0.07844558654084 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#16X4,#16X3+0:2]-[#7X3:3]-[#7X2:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t153" k1="2.855331016476 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#16X4,#16X3+0:2]=,:[#7X2:3]-,:[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t154" k1="3.281059187868 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[#6X4:1]-[#16X4,#16X3+0:2]-[#7X2:3]~[#6X3:4]" periodicity1="6" periodicity2="5" periodicity3="4" periodicity4="3" periodicity5="2" periodicity6="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" phase4="0.0 * degree ** 1" phase5="180.0 * degree ** 1" phase6="0.0 * degree ** 1" id="t155" k1="-0.2826023940207 * mole ** -1 * kilocalorie ** 1" k2="0.0615386171901 * mole ** -1 * kilocalorie ** 1" k3="0.2202095655706 * mole ** -1 * kilocalorie ** 1" k4="0.3103731336603 * mole ** -1 * kilocalorie ** 1" k5="-0.4012638405266 * mole ** -1 * kilocalorie ** 1" k6="2.157488864038 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0" idivf4="1.0" idivf5="1.0" idivf6="1.0"></Proper>
        <Proper smirks="[#8X1:1]~[#16X4,#16X3+0:2]-[#7X2:3]~[#6X3:4]" periodicity1="6" periodicity2="5" periodicity3="4" periodicity4="2" periodicity5="3" periodicity6="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="180.0 * degree ** 1" phase4="180.0 * degree ** 1" phase5="180.0 * degree ** 1" phase6="0.0 * degree ** 1" id="t156" k1="0.1650753721912 * mole ** -1 * kilocalorie ** 1" k2="-0.3117927347264 * mole ** -1 * kilocalorie ** 1" k3="0.2401800392794 * mole ** -1 * kilocalorie ** 1" k4="0.6581743876417 * mole ** -1 * kilocalorie ** 1" k5="1.234822415982 * mole ** -1 * kilocalorie ** 1" k6="1.185034498418 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0" idivf4="1.0" idivf5="1.0" idivf6="1.0"></Proper>
        <Proper smirks="[*:1]~[#16X4,#16X3+0:2]-[#8X2:3]-[*:4]" periodicity1="1" periodicity2="2" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t157" k1="3.051434888925 * mole ** -1 * kilocalorie ** 1" k2="-0.3442862669332 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#16X2,#16X3+1:2]-[#16X2,#16X3+1:3]-[*:4]" periodicity1="2" periodicity2="3" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t158" k1="3.627584495399 * mole ** -1 * kilocalorie ** 1" k2="0.3719185183686 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[*:1]-[#8X2:2]-[#15:3]~[*:4]" periodicity1="3" periodicity2="1" periodicity3="2" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t159" k1="0.4341790442654 * mole ** -1 * kilocalorie ** 1" k2="9.358349710903 * mole ** -1 * kilocalorie ** 1" k3="-1.887255581613 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[#8X2:1]-[#15:2]-[#8X2:3]-[#6X4:4]" periodicity1="3" periodicity2="2" periodicity3="1" phase1="0.0 * degree ** 1" phase2="0.0 * degree ** 1" phase3="0.0 * degree ** 1" id="t160" k1="-0.7552680418379 * mole ** -1 * kilocalorie ** 1" k2="-1.576662447943 * mole ** -1 * kilocalorie ** 1" k3="8.23237193769 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0" idivf3="1.0"></Proper>
        <Proper smirks="[*:1]~[#7:2]-[#15:3]~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" id="t161" k1="1.265542041067 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[#7:2]-[#15:3]=[*:4]" periodicity1="2" periodicity2="3" phase1="180.0 * degree ** 1" phase2="0.0 * degree ** 1" id="t162" k1="2.012364097137 * mole ** -1 * kilocalorie ** 1" k2="0.4145088037321 * mole ** -1 * kilocalorie ** 1" idivf1="1.0" idivf2="1.0"></Proper>
        <Proper smirks="[#6X3:1]-[#7:2]-[#15:3]=[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t163" k1="-1.94148850547 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[#7:2]=[#15:3]~[*:4]" periodicity1="3" phase1="0.0 * degree ** 1" id="t164" k1="-0.9670595402247 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]-[*:2]#[*:3]-[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t165" k1="0.0 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[*:2]-[*:3]#[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t166" k1="0.0 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
        <Proper smirks="[*:1]~[*:2]=[#6,#7,#16,#15;X2:3]=[*:4]" periodicity1="1" phase1="0.0 * degree ** 1" id="t167" k1="0.0 * mole ** -1 * kilocalorie ** 1" idivf1="1.0"></Proper>
    </ProperTorsions>
    <ImproperTorsions version="0.3" potential="k*(1+cos(periodicity*theta-phase))" default_idivf="auto">
        <Improper smirks="[*:1]~[#6X3:2](~[*:3])~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="5.230790565314 * mole ** -1 * kilocalorie ** 1" id="i1"></Improper>
        <Improper smirks="[*:1]~[#6X3:2](~[#8X1:3])~[#8:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="12.91569668378 * mole ** -1 * kilocalorie ** 1" id="i2"></Improper>
        <Improper smirks="[*:1]~[#7X3$(*~[#15,#16](!-[*])):2](~[*:3])~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="13.7015994787 * mole ** -1 * kilocalorie ** 1" id="i3"></Improper>
        <Improper smirks="[*:1]~[#7X3$(*~[#6X3]):2](~[*:3])~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="1.256262500552 * mole ** -1 * kilocalorie ** 1" id="i4"></Improper>
        <Improper smirks="[*:1]~[#7X3$(*~[#7X2]):2](~[*:3])~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="-2.341750027278 * mole ** -1 * kilocalorie ** 1" id="i5"></Improper>
        <Improper smirks="[*:1]~[#7X3$(*@1-[*]=,:[*][*]=,:[*]@1):2](~[*:3])~[*:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="16.00585907359 * mole ** -1 * kilocalorie ** 1" id="i6"></Improper>
        <Improper smirks="[*:1]~[#6X3:2](=[#7X2,#7X3+1:3])~[#7:4]" periodicity1="2" phase1="180.0 * degree ** 1" k1="10.12246975417 * mole ** -1 * kilocalorie ** 1" id="i7"></Improper>
    </ImproperTorsions>
    <vdW version="0.3" potential="Lennard-Jones-12-6" combining_rules="Lorentz-Berthelot" scale12="0.0" scale13="0.0" scale14="0.5" scale15="1.0" cutoff="9.0 * angstrom" switch_width="1.0 * angstrom" method="cutoff">
        <Atom smirks="[#1:1]" epsilon="0.0157 * mole ** -1 * kilocalorie ** 1" id="n1" rmin_half="0.6 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X4]" epsilon="0.01577948280971 * mole ** -1 * kilocalorie ** 1" id="n2" rmin_half="1.48419980825 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X4]-[#7,#8,#9,#16,#17,#35]" epsilon="0.01640924602775 * mole ** -1 * kilocalorie ** 1" id="n3" rmin_half="1.449786411317 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X4](-[#7,#8,#9,#16,#17,#35])-[#7,#8,#9,#16,#17,#35]" epsilon="0.0157 * mole ** -1 * kilocalorie ** 1" id="n4" rmin_half="1.287 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X4](-[#7,#8,#9,#16,#17,#35])(-[#7,#8,#9,#16,#17,#35])-[#7,#8,#9,#16,#17,#35]" epsilon="0.0157 * mole ** -1 * kilocalorie ** 1" id="n5" rmin_half="1.187 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X4]~[*+1,*+2]" epsilon="0.0157 * mole ** -1 * kilocalorie ** 1" id="n6" rmin_half="1.1 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X3]" epsilon="0.01561134320353 * mole ** -1 * kilocalorie ** 1" id="n7" rmin_half="1.443812569645 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X3]~[#7,#8,#9,#16,#17,#35]" epsilon="0.01310699839698 * mole ** -1 * kilocalorie ** 1" id="n8" rmin_half="1.377051329051 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X3](~[#7,#8,#9,#16,#17,#35])~[#7,#8,#9,#16,#17,#35]" epsilon="0.01479744504464 * mole ** -1 * kilocalorie ** 1" id="n9" rmin_half="1.370482808197 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#6X2]" epsilon="0.015 * mole ** -1 * kilocalorie ** 1" id="n10" rmin_half="1.459 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#7]" epsilon="0.01409081474669 * mole ** -1 * kilocalorie ** 1" id="n11" rmin_half="0.6192778454102 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#8]" epsilon="1.232599966667e-05 * mole ** -1 * kilocalorie ** 1" id="n12" rmin_half="0.2999999999997 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#16]" epsilon="0.0157 * mole ** -1 * kilocalorie ** 1" id="n13" rmin_half="0.6 * angstrom ** 1"></Atom>
        <Atom smirks="[#6:1]" epsilon="0.0868793154488 * mole ** -1 * kilocalorie ** 1" id="n14" rmin_half="1.953447017081 * angstrom ** 1"></Atom>
        <Atom smirks="[#6X2:1]" epsilon="0.21 * mole ** -1 * kilocalorie ** 1" id="n15" rmin_half="1.908 * angstrom ** 1"></Atom>
        <Atom smirks="[#6X4:1]" epsilon="0.1088406109251 * mole ** -1 * kilocalorie ** 1" id="n16" rmin_half="1.896698071741 * angstrom ** 1"></Atom>
        <Atom smirks="[#8:1]" epsilon="0.2102061007896 * mole ** -1 * kilocalorie ** 1" id="n17" rmin_half="1.706036917087 * angstrom ** 1"></Atom>
        <Atom smirks="[#8X2H0+0:1]" epsilon="0.1684651402602 * mole ** -1 * kilocalorie ** 1" id="n18" rmin_half="1.697783613804 * angstrom ** 1"></Atom>
        <Atom smirks="[#8X2H1+0:1]" epsilon="0.2094735324129 * mole ** -1 * kilocalorie ** 1" id="n19" rmin_half="1.682099169199 * angstrom ** 1"></Atom>
        <Atom smirks="[#7:1]" epsilon="0.1676915150424 * mole ** -1 * kilocalorie ** 1" id="n20" rmin_half="1.799798315098 * angstrom ** 1"></Atom>
        <Atom smirks="[#16:1]" epsilon="0.25 * mole ** -1 * kilocalorie ** 1" id="n21" rmin_half="2.0 * angstrom ** 1"></Atom>
        <Atom smirks="[#15:1]" epsilon="0.2 * mole ** -1 * kilocalorie ** 1" id="n22" rmin_half="2.1 * angstrom ** 1"></Atom>
        <Atom smirks="[#9:1]" epsilon="0.061 * mole ** -1 * kilocalorie ** 1" id="n23" rmin_half="1.75 * angstrom ** 1"></Atom>
        <Atom smirks="[#17:1]" epsilon="0.2656001046527 * mole ** -1 * kilocalorie ** 1" id="n24" rmin_half="1.85628721824 * angstrom ** 1"></Atom>
        <Atom smirks="[#35:1]" epsilon="0.3218986365974 * mole ** -1 * kilocalorie ** 1" id="n25" rmin_half="1.969806594135 * angstrom ** 1"></Atom>
        <Atom smirks="[#53:1]" epsilon="0.4 * mole ** -1 * kilocalorie ** 1" id="n26" rmin_half="2.35 * angstrom ** 1"></Atom>
        <Atom smirks="[#3+1:1]" epsilon="0.0279896 * mole ** -1 * kilocalorie ** 1" id="n27" rmin_half="1.025 * angstrom ** 1"></Atom>
        <Atom smirks="[#11+1:1]" epsilon="0.0874393 * mole ** -1 * kilocalorie ** 1" id="n28" rmin_half="1.369 * angstrom ** 1"></Atom>
        <Atom smirks="[#19+1:1]" epsilon="0.1936829 * mole ** -1 * kilocalorie ** 1" id="n29" rmin_half="1.705 * angstrom ** 1"></Atom>
        <Atom smirks="[#37+1:1]" epsilon="0.3278219 * mole ** -1 * kilocalorie ** 1" id="n30" rmin_half="1.813 * angstrom ** 1"></Atom>
        <Atom smirks="[#55+1:1]" epsilon="0.4065394 * mole ** -1 * kilocalorie ** 1" id="n31" rmin_half="1.976 * angstrom ** 1"></Atom>
        <Atom smirks="[#9X0-1:1]" epsilon="0.003364 * mole ** -1 * kilocalorie ** 1" id="n32" rmin_half="2.303 * angstrom ** 1"></Atom>
        <Atom smirks="[#17X0-1:1]" epsilon="0.035591 * mole ** -1 * kilocalorie ** 1" id="n33" rmin_half="2.513 * angstrom ** 1"></Atom>
        <Atom smirks="[#35X0-1:1]" epsilon="0.0586554 * mole ** -1 * kilocalorie ** 1" id="n34" rmin_half="2.608 * angstrom ** 1"></Atom>
        <Atom smirks="[#53X0-1:1]" epsilon="0.0536816 * mole ** -1 * kilocalorie ** 1" id="n35" rmin_half="2.86 * angstrom ** 1"></Atom>
        <Atom smirks="[#1]-[#8X2H2+0:1]-[#1]" epsilon="0.1521 * mole ** -1 * kilocalorie ** 1" id="n-tip3p-O" sigma="3.1507 * angstrom ** 1"></Atom>
        <Atom smirks="[#1:1]-[#8X2H2+0]-[#1]" epsilon="0.0 * mole ** -1 * kilocalorie ** 1" id="n-tip3p-H" sigma="1 * angstrom ** 1"></Atom>
        <Atom smirks="[#54:1]" epsilon="0.561 * kilocalorie ** 1 * mole ** -1" id="n36" sigma="4.363 * angstrom ** 1"></Atom>
    </vdW>
    <Electrostatics version="0.3" scale12="0.0" scale13="0.0" scale14="0.8333333333" scale15="1.0" cutoff="9.0 * angstrom" switch_width="0.0 * angstrom" method="PME"></Electrostatics>
    <ToolkitAM1BCC version="0.3"></ToolkitAM1BCC>
</SMIRNOFF>
"""


if __name__ == "__main__":
    main()

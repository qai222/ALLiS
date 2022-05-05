import datetime
import subprocess
from functools import reduce
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd

from lsal.schema import Molecule
from lsal.utils import to_float, json_load, file_exists

"""
Three calculators are used:
1. `cxcalc` in CHEMAXON: 
    - remember to add the `bin` folder to `PATH`
    - list of descriptors https://docs.chemaxon.com/display/docs/cxcalc-calculator-functions.md

2. `mordred` by Moriwaki:
    - cite 10.1186/s13321-018-0258-y
    - list of descriptors https://mordred-descriptor.github.io/documentation/master/descriptors.html

3. `opera` (pKa only) by Mansouri:
    - cite 10.1186/s13321-019-0384-1
"""

_cxcalc_descriptors = """
# polarizability
avgpol axxpol ayypol azzpol molpol dipole 
# surface
asa maximalprojectionarea maximalprojectionradius minimalprojectionarea minimalprojectionradius psa vdwsa volume   
# count
chainatomcount chainbondcount fsp3 fusedringcount rotatablebondcount acceptorcount accsitecount donorcount donsitecount mass
# topological
hararyindex balabanindex hyperwienerindex wienerindex wienerpolarity
"""
_cxcalc_descriptors = [l for l in _cxcalc_descriptors.strip().split("\n") if not l.startswith("#")]
_cxcalc_descriptors = [l.split() for l in _cxcalc_descriptors]
_cxcalc_descriptors = reduce(lambda x, y: x + y, _cxcalc_descriptors)


def calculate_cxcalc(bin: Union[Path, str] = "cxcalc.exe", mol_file="mols_test.smi",
                     descriptors: list[str] = _cxcalc_descriptors) -> pd.DataFrame:
    result = subprocess.run([bin, ] + [mol_file, ] + descriptors, capture_output=True)
    data = result.stdout.decode("utf-8").strip()

    lines = data.split("\n")
    n_cols = len(lines[1].split())

    colnames = ["id"] + descriptors.copy()
    if "asa" in colnames:
        asa_index = colnames.index("asa")
        # accessible surface area given by positive (ASA+) and negative (ASA À ) partial charges on atoms
        # and also surface area induced by hydrophobic (SA_H) and polar (SA_P) atoms
        colnames = colnames[:asa_index] + ["ASA+", "ASA-", "ASA_H", "ASA_P"] + colnames[asa_index:]
    assert len(colnames) == n_cols

    lines = lines[1:]
    values = np.zeros((len(lines), len(colnames)))
    for i in range(0, len(lines)):
        line = lines[i]
        values[i] = [float(v) for v in line.split()]
    df = pd.DataFrame(data=values, columns=colnames)
    df.pop("id")
    assert not df.isnull().any().any()
    return df


_mordred_descriptors = [
    "SLogP", "nHeavyAtom", "fragCpx", "nC", "nO", "nN", "nP", "nS", "nRing",
]


def calculate_mordred(smis: list[str], descriptor_names=_mordred_descriptors) -> pd.DataFrame:
    from mordred import Calculator, descriptors, Descriptor
    from rdkit import Chem
    used_descriptors = []
    for des in Calculator(descriptors).descriptors:
        des: Descriptor
        if des.__str__() in descriptor_names:
            used_descriptors.append(des)
    assert len(used_descriptors) == len(descriptor_names)
    calc = Calculator(used_descriptors)
    mols = [Chem.MolFromSmiles(smi) for smi in smis]
    df = calc.pandas(mols)
    assert not df.isnull().any().any()
    return df


def opera_pka(opera_output: Union[Path, str] = "mols-smi_OPERA2.7Pred.csv") -> pd.DataFrame:
    """
    use opera to predict aqueous pKa of 2d molecular structures
    1. download opera release 2.7 from https://github.com/kmansouri/OPERA
    2. write molecules to mols.smi file
    3. run opera pka predictor, output to csv
    4. run this function to read csv
    """
    # parse output
    opera_df = pd.read_csv(opera_output)
    opera_df = opera_df[["pKa_a_pred", "pKa_b_pred"]]

    records = []
    for r in opera_df.to_dict(orient="records"):
        pka = to_float(r["pKa_a_pred"])
        pkb = to_float(r["pKa_b_pred"])
        assert not (pka is None and pkb is None)
        if pka is None:
            is_acidic = 0
            p = pkb
        else:
            is_acidic = 1
            p = pka
        records.append({"is_acidic": is_acidic, "pKa": p})
    return pd.DataFrame.from_records(records)


if __name__ == '__main__':

    mols_file = "ligand_descriptors.smi"
    mols = json_load("ligand_inventory.json")
    if not file_exists(mols_file):
        Molecule.write_molecules(mols, mols_file, "smi")  # write smi file
    mordred_df = calculate_mordred(smis=[m.smiles for m in mols])
    cxcalc_df = calculate_cxcalc(mol_file=mols_file)
    pka_df = opera_pka("ligand_descriptors_OPERA2.7Pred.csv")

    des_df = pd.concat([pka_df, cxcalc_df, mordred_df], axis=1)
    des_df["InChI"] = [m.inchi for m in mols]
    des_df["IUPAC Name"] = [m.iupac_name for m in mols]
    des_df.to_csv("ligand_descriptors_{}.csv".format(datetime.datetime.now().strftime("%Y_%m_%d")), index=False)

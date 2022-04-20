import abc
import itertools
import logging
import os
import pathlib
import typing

import mordred
import pandas as pd
from monty.json import MSONable

from lsal.utils import inchi2smiles, smiles2inchi, MolFromInchi


class Molecule(MSONable):

    def __init__(self, inchi: str, iupac_name: str = "unknwon", name="unknown", label=None):
        self.inchi = inchi.strip()
        self.iupac_name = iupac_name.strip()
        self.name = name.strip()
        self.label = label

    @property
    def rdmol(self):
        return MolFromInchi(self.inchi)

    @property
    def smiles(self) -> str:
        return inchi2smiles(self.inchi)

    @classmethod
    def from_str(cls, s: str, repr_type="inchi", iupac_name="unknown", name="unknown", label=None):
        if repr_type.startswith("i"):
            return cls(s, iupac_name=iupac_name, name=name, label=label)
        elif repr_type.startswith("s"):
            return cls(smiles2inchi(s), iupac_name=iupac_name, name=name, label=label)
        else:
            raise NotImplementedError("`repr_type` not implemented: {}".format(repr_type))

    def __repr__(self):
        return self.inchi + "---" + self.iupac_name + "---" + self.name + "--- {}".format(self.label)

    def __str__(self):
        return "{}: {} -- {} -- {} -- {}".format(self.__class__.__name__, self.inchi, self.iupac_name, self.name, self.label)

    @classmethod
    def from_repr(cls, s: str):
        inchi, iupac_name, name, label = s.split("---")
        return cls(inchi, iupac_name, name, label)

    def __hash__(self):
        return hash(self.inchi)

    def __eq__(self, other):
        return self.inchi == other.inchi

    def __gt__(self, other):
        return self.__repr__().__gt__(other.__repr__())

    def __lt__(self, other):
        return self.__repr__().__lt__(other.__repr__())

    def as_flat_dict(self) -> dict:
        # TODO export a flat dict for generating csv
        pass

    @staticmethod
    def write_smi(mols, fn):
        with open(fn, "w") as f:
            f.write("\n".join([m.smiles for m in mols]))


def molecule_from_name(name: str, inventory:list[Molecule]) -> Molecule:
    for m in inventory:
        if m.name == name:
            return m
    raise ValueError("name not found in the inventory: {}".format(name))

def molecule_from_label(label:int, inventory:list[Molecule]) -> Molecule:
    for m in inventory:
        if m.label == label:
            return m
    raise ValueError("label not found in the inventory: {}".format(label))

class Descriptor(MSONable):

    def __init__(self, name: str, source: str, description: str = None, parameters: dict = None):
        self.name = name
        self.source = source
        if description is None:
            description = name
        self.description = description
        if parameters is None:
            parameters = dict()
        self.parameters = parameters

    def __repr__(self):
        return "{} -- {}".format(self.source, self.name)

    def __hash__(self):
        # TODO also hash params
        return hash(self.name + self.source)

    @classmethod
    def from_mordred_descriptor(cls, des: mordred.Descriptor):
        params = des.get_parameter_dict()

        for k, v in params.items():
            if any(isinstance(v, t) for t in (float, str, int)):
                continue
            elif v is None:
                continue
            else:
                params[k] = v.description()

        name = str(des)
        decription = des.__doc__
        return cls(name, "MORDRED-{}".format(mordred.__version__), decription, params)


def load_inventory(fn: typing.Union[pathlib.Path, str], to_mols=True):
    assert os.path.isfile(fn)
    _, extension = os.path.splitext(fn)
    if extension == ".csv":
        df = pd.read_csv(fn)
    elif extension == ".xlsx":
        df = pd.read_excel(fn)
    else:
        raise AssertionError("inventory file should be either csv or xlsx")
    assert "InChI" in df.columns, "InChI must be specified in the inventory"
    df = df.dropna(axis=0, how="all", subset=["InChI"])
    if to_mols:
        return inventory_df_to_mols(df)
    else:
        return df


def inventory_df_to_mols(df:pd.DataFrame) -> [Molecule]:
    mols = []
    for row in df.to_dict("records"):
        inchi = row["InChI"]
        name = "unknown"
        iupac_name = "unknown"
        label = None
        try:
            name = row["Name"]
        except KeyError:
            pass
        try:
            iupac_name = row["IUPAC Name"]
        except KeyError:
            pass
        try:
            label = int(row["LigandLabel"])
        except KeyError:
            pass
        m = Molecule.from_str(inchi, "inchi", iupac_name=iupac_name, name=name, label=label)
        mols.append(m)
    return mols


class ReactionCondition(MSONable):
    def __init__(self, name: str, value: float or int):
        assert set(name).issuperset({"(", ")"}), "a bracketed unit should present in the name of a condition!"
        self.value = value
        self.name = name

    def as_flat_dict(self) -> dict:
        # TODO export a flat dict for generating csv
        pass

    def __hash__(self):
        return hash((self.name, self.value))

    def __repr__(self):
        s = "{}: ".format(self.__class__.__name__)
        for k, v in self.as_dict().items():
            if k.startswith("@"):
                continue
            if isinstance(v, float):
                v = "{:.4f}".format(v)
            s += "{}=={}\t".format(k, v)
        return s

    def check(self):
        pass

    def __gt__(self, other):
        return self.__repr__().__gt__(other.__repr__())

    def __lt__(self, other):
        return self.__repr__().__lt__(other.__repr__())

    def __eq__(self, other):
        return self.__hash__() == other.__hash__()


class Reactant(MSONable):
    def __init__(self, identity: Molecule or str, properties: dict = None):
        self.identity = identity
        if properties is None:
            properties = dict()
        self.properties = properties

    def as_flat_dict(self) -> dict:
        # TODO export a flat dict for generating csv
        pass

    def __hash__(self):
        # TODO hmmmm....
        return hash(self.__repr__())

    def __repr__(self):
        s = "{}: ".format(self.__class__.__name__)
        for k, v in self.as_dict().items():
            if k.startswith("@"):
                continue
            if isinstance(v, float):
                v = "{:.4f}".format(v)
            s += "{}=={}\t".format(k, v)
        return s

    def check(self):
        for k, v in self.as_dict().items():
            if k.startswith("@"):
                continue
            if v is None:
                logging.warning("{}: {} is {}!".format(self.__repr__(), k, v))

    def __gt__(self, other):
        return self.__repr__().__gt__(other.__repr__())

    def __lt__(self, other):
        return self.__repr__().__lt__(other.__repr__())

    def __eq__(self, other):
        return self.__repr__() == other.__repr__()


class ReactantSolvent(Reactant):
    def __init__(self, identity: Molecule or str, volume: float, volume_unit: str = "ul", properties: dict = None):
        super().__init__(identity, properties)
        self.volume_unit = volume_unit
        self.volume = volume


class ReactantSolution(Reactant):
    def __init__(self, identity: Molecule or str, volume: float, concentration: float or None,
                 solvent_identity: Molecule or str,
                 properties: dict = None, volume_unit: str = "ul", concentration_unit: str or None = "M"):
        super().__init__(identity, properties)
        self.volume_unit = volume_unit
        self.concentration_unit = concentration_unit
        self.solvent_identity = solvent_identity
        self.concentration = concentration
        self.volume = volume


class GeneralReaction(MSONable, abc.ABC):
    def __init__(self, identifier: str, reactants: [Reactant], conditions: [ReactionCondition],
                 properties: dict = None):
        if properties is None:
            properties = dict()
        self.properties = properties
        self.reactants = reactants
        self.conditions = conditions
        self.identifier = identifier

    def check(self):
        logging.warning("checking reaction: {}".format(self.identifier))
        for r in self.reactants:
            r.check()
        for c in self.conditions:
            c.check()

    def as_flat_dict(self) -> dict:
        # TODO export a flat dict for generating csv
        pass

    def __hash__(self):
        return hash(self.identifier)

    def __repr__(self):
        s = "{}:\n".format(self.__class__.__name__)
        for k, v in self.as_dict().items():
            if k.startswith("@"):
                continue
            s += "\t{}: {}\n".format(k, v)
        return s

    def __gt__(self, other):
        return self.__repr__().__gt__(other.__repr__())

    def __lt__(self, other):
        return self.__repr__().__lt__(other.__repr__())

    def __eq__(self, other):
        return self.__repr__() == other.__repr__()


def group_reactions(reactions: [GeneralReaction], field: str):
    from lsal.utils import rgetattr
    groups = []
    unique_keys = []
    keyfunc = lambda x: rgetattr(x, field)
    rs = sorted(reactions, key=keyfunc)
    for k, g in itertools.groupby(rs, key=keyfunc):
        groups.append(list(g))
        unique_keys.append(k)
    return unique_keys, groups
from __future__ import annotations

import abc
import itertools
from copy import deepcopy
from typing import Tuple, List, Iterable, Union

from loguru import logger
from monty.json import MSONable

from lsal.schema.material import Molecule, NanoCrystal
from lsal.utils import msonable_repr, rgetattr

_Precision = 5
_EPS = 1 ** -_Precision


class ReactionInfo(MSONable, abc.ABC):

    def __init__(self, properties: dict = None):
        if properties is None:
            properties = dict()
        self.properties = properties

    def __repr__(self):
        return msonable_repr(self, precision=_Precision)

    def __hash__(self):
        return hash(self.__repr__())

    def __gt__(self, other):
        return self.__repr__().__gt__(other.__repr__())

    def __lt__(self, other):
        return self.__repr__().__lt__(other.__repr__())

    def __eq__(self, other):
        return self.__repr__() == other.__repr__()

    def check(self):
        for k, v in self.as_dict().items():
            if k.startswith("@"):
                continue
            if v is None:
                logger.warning("{}: {} is {}!".format(self.__repr__(), k, v))


class ReactionCondition(ReactionInfo):
    def __init__(self, name: str, value: Union[float, int], properties: dict = None):
        super().__init__(properties)
        assert set(name).issuperset({"(", ")"}), "a bracketed unit should present in the name of a condition!"
        self.value = value
        self.name = name


class ReactantSolution(ReactionInfo):
    def __init__(self, solute: Union[NanoCrystal, Molecule], volume: float, concentration: float or None,
                 solvent: Molecule,
                 volume_unit: str, concentration_unit: str or None, properties: dict = None, ):
        super().__init__(properties)
        self.solute = solute
        self.solvent = solvent
        self.concentration = concentration  # concentration can be None for e.g. nanocrystal
        self.volume = volume
        if self.concentration == 0:
            assert self.solvent == self.solute, "zero concentration but different solvent and solute: {} - {}".format(
                self.solvent, self.solute)
        self.volume_unit = volume_unit
        self.concentration_unit = concentration_unit

    @property
    def amount(self) -> float:
        return self.concentration * self.volume

    @property
    def amount_unit(self) -> str:
        return "{}*{}".format(self.volume_unit, self.concentration_unit)

    @property
    def is_solvent(self) -> bool:
        return self.concentration == 0


class GeneralReaction(MSONable, abc.ABC):
    def __init__(self, identifier: str, reactants: list[ReactantSolution], conditions: list[ReactionCondition],
                 properties: dict = None):
        if properties is None:
            properties = dict()
        self.properties = properties
        self.reactants = reactants
        self.conditions = conditions
        self.identifier = identifier

    def __repr__(self):
        s = "Reaction: {}\n".format(self.identifier)
        for k, v in self.properties.items():
            s += "\t Property: {} = {}\n".format(k, v)
        for reactant in self.reactants:
            s += "\t Reactant: {}\n".format(reactant.__repr__())
        for condition in self.conditions:
            s += "\t Condition: {}\n".format(condition.__repr__())
        return s

    def __hash__(self):
        return hash(self.identifier)

    def __gt__(self, other):
        return self.identifier.__gt__(other.identifier)

    def __lt__(self, other):
        return self.identifier.__lt__(other.identifier)

    def __eq__(self, other):
        return self.identifier == other.identifier

    def check(self):
        logger.warning("checking reaction: {}".format(self.identifier))
        for r in self.reactants:
            r.check()
        for c in self.conditions:
            c.check()

    @staticmethod
    def group_reactions(reactions: Iterable[GeneralReaction], field: str):
        """ group reactions by a field, the field can be dot-structured, e.g. "nc_solution.solute" """
        groups = []
        unique_keys = []

        def keyfunc(x):
            return rgetattr(x, field)

        rs = sorted(reactions, key=keyfunc)
        for k, g in itertools.groupby(rs, key=keyfunc):
            groups.append(list(g))
            unique_keys.append(k)
        return unique_keys, groups


class LXReaction(GeneralReaction):

    def __init__(
            self,
            identifier: str,
            conditions: list[ReactionCondition],
            solvent: ReactantSolution = None,
            nc_solution: ReactantSolution = None,
            ligand_solutions: list[ReactantSolution] = None,
            properties: dict = None,
    ):
        super().__init__(identifier, ligand_solutions + [solvent, nc_solution], conditions, properties)
        self.solvent = solvent
        self.nc_solution = nc_solution
        self.ligand_solutions = ligand_solutions
        assert len(self.ligand_tuple) == len(
            set(self.ligand_tuple)), "one solution for one ligand, " \
                                     "but we have # solutions vs # ligands: {} vs {}".format(
            len(set(self.ligand_tuple)), len(self.ligand_tuple))
        assert self.solvent.is_solvent, "the solvent given is not really a solvent: {}".format(self.solvent)

    @property
    def is_reaction_nc_reference(self) -> bool:
        """ whether the reaction is a reference reaction in which only NC solution and solvent were added """
        nc_good = self.nc_solution is not None and self.nc_solution.volume > _EPS
        solvent_good = self.solvent is not None and self.solvent.volume > _EPS
        no_ligand = len(self.ligand_solutions) == 0 or all(
            ls is None or ls.volume < _EPS for ls in self.ligand_solutions)
        return nc_good and solvent_good and no_ligand

    @property
    def is_reaction_blank_reference(self) -> bool:
        """ whether the reaction is a reference reaction in which only solvent was added """
        no_nc = self.nc_solution is None or self.nc_solution.volume < _EPS
        solvent_good = self.solvent is not None and self.solvent.volume > _EPS
        no_ligand = len(self.ligand_solutions) == 0 or all(
            ls is None or ls.volume < _EPS for ls in self.ligand_solutions)
        return no_nc and no_ligand and solvent_good

    @property
    def is_reaction_real(self) -> bool:
        """ whether the reaction is neither a blank nor a ref """
        return not self.is_reaction_blank_reference and not self.is_reaction_nc_reference

    @property
    def ligand_tuple(self) -> Tuple[Molecule, ...]:
        return tuple(sorted([ls.solute for ls in self.ligand_solutions]))

    @property
    def unique_ligands(self) -> Tuple[Molecule, ...]:
        return tuple(sorted(set(self.ligand_tuple)))


class L1XReaction(LXReaction):
    @property
    def ligand(self):
        try:
            return self.ligand_tuple[0]
        except IndexError:
            return None

    @property
    def ligand_solution(self):
        try:
            return self.ligand_solutions[0]
        except IndexError:
            return None


class L1XReactionCollection(MSONable):
    # TODO the reactions in a collection should have something in common (e.g. solvent/mixing conditions)
    def __init__(self, reactions: List[L1XReaction], properties: dict = None):
        self.reactions = reactions
        if properties is None:
            properties = dict()
        self.properties = properties

    @property
    def identifiers(self) -> Tuple[str]:
        return tuple([r.identifier for r in self.reactions])

    @property
    def ref_reactions(self):
        reactions = []
        for r in self.reactions:
            if r.is_reaction_nc_reference:
                reactions.append(r)
            else:
                continue
        return reactions

    def get_reference_reactions(self, reaction: L1XReaction) -> list[L1XReaction]:
        # given a reaction return its corresponding reference reactions
        # i.e. same identifier
        refs = []
        for ref_r in self.ref_reactions:
            if ref_r.identifier.split("@@")[0] == reaction.identifier.split("@@")[0]:
                refs.append(ref_r)
        return refs

    @property
    def ligand_amount_range(self):
        amounts = []
        amount_unit = []
        for r in self.real_reactions:
            amounts.append(r.ligand_solution.amount)
            amount_unit.append(r.ligand_solution.amount_unit)
        assert len(set(amount_unit)) == 1
        return min(amounts), max(amounts), amount_unit[0]

    @classmethod
    def subset_by_ligands(cls, campaign_reactions: L1XReactionCollection, allowed_ligands: List[Molecule]):
        """ select real reactions by allowed ligands """
        reactions = [r for r in campaign_reactions.real_reactions if r.ligand in allowed_ligands]
        return cls(reactions, properties=deepcopy(campaign_reactions.properties))

    @property
    def real_reactions(self) -> list[L1XReaction]:
        reactions = []
        for r in self.reactions:
            if r.is_reaction_real:
                reactions.append(r)
            else:
                continue
        return reactions

    @property
    def ligands(self) -> List[Molecule]:
        return [r.ligand for r in self.real_reactions]

    @property
    def unique_ligands(self) -> list:
        return sorted(set(self.ligands))

    def __repr__(self):
        s = "{}\n".format(self.__class__.__name__)
        s += "\t# of reactions: {}\n".format(len(self.reactions))
        s += "\t# of ligands: {}\n".format(len(self.unique_ligands))
        return s

    def ligand_to_reactions_mapping(self, limit_to: Iterable[Molecule] = None) -> dict[Molecule, list[L1XReaction]]:
        reactions = self.real_reactions
        ligands, grouped_reactions = L1XReaction.group_reactions(reactions, field="unique_ligands")
        ligand_to_reactions = dict(zip(ligands, grouped_reactions))
        if limit_to is None:
            limit_to = ligands
        return {c: ligand_to_reactions[c] for c in limit_to}

    @staticmethod
    def assign_reaction_results(reactions: list[L1XReaction], peak_data: dict[str, dict]):
        assert len(peak_data) == len(reactions)
        for r in reactions:
            data = peak_data[r.identifier]
            r.properties.update(data)

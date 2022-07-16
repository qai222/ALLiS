import matplotlib.pyplot as plt
import numpy as np

from lsal.campaign.loader import ReactionCollection, LigandExchangeReaction, Molecule
from lsal.utils import json_load, get_basename


def get_od(r: LigandExchangeReaction):
    for k in r.properties:
        if k.strip("'").endswith("_PL_OD390"):
            v = r.properties[k]
            assert isinstance(v, float)
            return v
    raise KeyError


def get_sumod(r: LigandExchangeReaction):
    for k in r.properties:
        if k.strip("'").endswith("_PL_sum/OD390"):
            v = r.properties[k]
            assert isinstance(v, float)
            return v
    raise KeyError


def get_target_data(reaction_collection: ReactionCollection, get_function=get_od):
    ligand_to_reactions = reaction_collection.get_lcomb_to_reactions()
    ligand_to_reactions = {k[0]: v for k, v in ligand_to_reactions.items()}
    data = dict()
    for ligand, reactions in ligand_to_reactions.items():
        amounts = []
        amount_units = []
        values = []
        ref_values = []
        for r in reactions:
            r: LigandExchangeReaction
            reference_reactions = reaction_collection.get_reference_reactions(r)
            ref_values += [get_function(refr) for refr in reference_reactions]
            amount = r.ligand_solutions[0].amount
            amount_unit = r.ligand_solutions[0].amount_unit
            amount_units.append(amount_unit)
            value = get_function(r)
            amounts.append(amount)
            values.append(value)
        data[ligand] = {"amount": amounts, "amount_unit": amount_units[0], "values": values, "ref_values": ref_values}
    return data


def plot_od_persis(od_data: dict[Molecule, dict[str, list[float]]], ylabel="OD"):
    ligands = sorted(od_data.keys())
    ncols = 5
    nrows = len(ligands) // ncols + 1
    fig, total_axes = plt.subplots(nrows=nrows, ncols=ncols,
                                   figsize=(4 * nrows, 4 * ncols,))
    for i in range(nrows):
        for j in range(ncols):
            total_axes[i][j].set_axis_off()

    for iax, ligand in enumerate(ligands):
        ax = total_axes[iax // ncols][iax % ncols]
        ax.set_axis_on()
        data = od_data[ligand]
        xs = data["amount"]
        ys = data["values"]
        y_ref = np.mean(data["ref_values"])
        y_ref_err = np.std(data["ref_values"])
        # y_ref_err = max(data["ref_OD"]) - min(data["ref_OD"])
        y_ref = np.array([y_ref, ] * len(xs))
        ax.scatter(xs, ys, marker="x", c="k", label="Experimental")
        ax.fill_between(sorted(xs), y_ref - 3 * y_ref_err, y_ref + 3 * y_ref_err, alpha=0.2, label=r"ref $3\delta$")
        ax.set_title(ligand.label)
        ax.set_xscale("log")
        # ax.set_ylim([-0.2, 2.3])
        ax.set_xlabel("amount (uL*uM)")
        ax.set_ylabel(ylabel)
        if iax == 0:
            ax.legend()
    fig.subplots_adjust(hspace=0.5, wspace=0.3)
    return fig


if __name__ == '__main__':
    reactions = json_load("../data/collect_reactions_SL_0519.json")

    od_data = get_target_data(reactions, get_od)
    fig_od = plot_od_persis(od_data, ylabel="OD")
    fig_od.savefig(get_basename(__file__) + "_od.png")

    sumod_data = get_target_data(reactions, get_sumod)
    fig_sumod = plot_od_persis(sumod_data, ylabel="sum/OD")
    fig_sumod.savefig(get_basename(__file__) + "_sumod.png")
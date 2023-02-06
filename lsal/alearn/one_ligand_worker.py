import glob
from os.path import abspath

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from loguru import logger
from matplotlib import patches
from tqdm import tqdm

from lsal.alearn import SingleLigandLearner, SingleLigandPrediction, QueryRecord
from lsal.schema import Worker, L1XReactionCollection
from lsal.utils import log_time, json_load, json_dump, pkl_dump, \
    pkl_load, chunks, createdir, file_exists


def _fix_hist_step_vertical_line_at_end(ax):
    """ https://stackoverflow.com/questions/39728723/ """
    axpolygons = [poly for poly in ax.get_children() if isinstance(poly, patches.Polygon)]
    for poly in axpolygons:
        poly.set_xy(poly.get_xy()[:-1])


class OneLigandWorker(Worker):

    def __init__(
            self,
            # teach
            code_dir,
            work_dir,
            reaction_collection_json,
            prediction_ligand_pool_json,

            learner_wdir="./",
            prediction_dir="./prediction/",
            ranking_df_dir="./ranking_df/",
            suggestion_dir="./suggestion/",

            model_path="./TwinRF_model.pkl",
            learner_json="./learner.json.gz",
            query_json=f"query_record.json.gz",
            ranking_dataframe_csv="./ranking_df/qr_ranking.csv",

            # complexity cutoff becomes inappropriate when found cas/total == 29926/43724
            complexity_descriptor='complexity_BertzCT',

            test_predict: int = None,
    ):
        super().__init__(name=self.__class__.__name__, code_dir=code_dir, work_dir=work_dir)
        self.ranking_df_dir = ranking_df_dir
        self.test_predict = test_predict
        self.prediction_ligand_pool_json = prediction_ligand_pool_json
        self.ranking_dataframe_csv = ranking_dataframe_csv
        self.complexity_descriptor = complexity_descriptor
        self.suggestion_dir = suggestion_dir
        self.query_json = query_json
        self.prediction_dir = prediction_dir
        self.learner_json = learner_json
        self.model_path = model_path
        self.learner_wdir = learner_wdir
        self.reaction_collection_json = reaction_collection_json

        self.complexity_cutoff = None

    @log_time
    def teach(self):
        reactions = []
        for rc_json in self.reaction_collection_json:
            rc = json_load(rc_json, gz=True)
            rc: L1XReactionCollection
            reactions += rc.real_reactions
        reaction_collection = L1XReactionCollection(reactions)

        learner = SingleLigandLearner.init_trfr(
            teaching_figure_of_merit='FigureOfMerit',
            wdir=self.learner_wdir,
        )

        learner.teach_reactions(reaction_collection, self.model_path, tune=False, split_in_tune=True)
        json_dump(learner, self.learner_json, gz=True)

    @log_time
    def predict(self):
        ligand_pool = json_load(self.prediction_ligand_pool_json, gz=True)
        logger.warning(f"making predictions for # of ligands: {len(ligand_pool)}")
        if self.test_predict:
            ligand_pool = ligand_pool[:self.test_predict]
        npreds = 200

        learner = json_load(self.learner_json, gz=True)
        learner: SingleLigandLearner
        ligand_amounts = learner.latest_teaching_record.reaction_collection.amount_geo_space(npreds)

        learner.load_model(-1)

        createdir(self.prediction_dir)
        chunk_size = 10
        for ichunk, lig_chunk in enumerate(tqdm(list(chunks(ligand_pool, chunk_size)))):

            save_as = self.prediction_dir + "/prediction_chunk_{0:06d}.pkl".format(ichunk)
            if file_exists(save_as):
                prediction_chunk = pkl_load(save_as)
                for slp, li in zip(prediction_chunk, lig_chunk):
                    slp: SingleLigandPrediction
                    assert slp.ligand == li
            else:
                prediction_chunk = learner.predict(
                    lig_chunk, ligand_amounts
                )
                pkl_dump(prediction_chunk, save_as)

    @log_time
    def query(self):
        ligands = []
        rkdfs = []
        for pkl in tqdm(sorted(glob.glob(f"{self.prediction_dir}/prediction_*.pkl"))):
            slps = pkl_load(pkl, print_timing=False)
            slps: list[SingleLigandPrediction]
            ligands += [p.ligand for p in slps]
            rkdf = SingleLigandPrediction.calculate_ranking([p.ligand for p in slps], slps, )
            rkdfs.append(rkdf)
        rkdf = pd.concat(rkdfs, axis=0, ignore_index=True)
        qr = SingleLigandPrediction.query(ligands, rkdf, self.model_path)
        json_dump(qr, self.query_json, gz=True)
        # self.collect_files.append(abspath(self.query_json))

    @log_time
    def ranking_dataframe(self):
        createdir(self.ranking_df_dir)

        top_percentile = 2
        top_percent = top_percentile * 0.01

        # mark already taught ligands
        reactions = []
        for rc_json in self.reaction_collection_json:
            rc = json_load(rc_json, gz=True)
            rc: L1XReactionCollection
            reactions += rc.reactions
        reaction_collection = L1XReactionCollection(reactions)
        taught_ligand_identifiers = [lig.identifier for lig in reaction_collection.ligands]

        # load query record
        qr = json_load(self.query_json, gz=True)
        qr: QueryRecord
        ligand_pool = {lig.identifier: lig for lig in qr.pool}

        # Molecule.write_molecules(list(ligand_pool.values()), f"{self.ranking_df_dir}/ligand_pool.csv", "csv")

        ranking_dataframe = qr.ranking_dataframe
        ranking_dataframe: pd.DataFrame

        ranking_dataframe = ranking_dataframe.loc[ranking_dataframe['ligand_identifier'].isin(list(ligand_pool.keys()))]
        new_records = []
        for record in ranking_dataframe.to_dict(orient='records'):
            identifier = record['ligand_identifier']
            record.update(
                {
                    self.complexity_descriptor: ligand_pool[identifier].properties[self.complexity_descriptor],
                    'is_taught': identifier in taught_ligand_identifiers,
                    'cas_number': ligand_pool[identifier].properties['cas_number']
                }
            )
            new_records.append(record)
        ranking_dataframe = pd.DataFrame.from_records(new_records)
        ranking_dataframe.to_csv(self.ranking_dataframe_csv, index=False)
        self.collect_files.append(abspath(self.ranking_dataframe_csv))

        for rank_method in [c for c in ranking_dataframe.columns if c.startswith('rank_')]:
            fig, ax = plt.subplots()
            ax.set_xlabel(rank_method)
            ax.set_ylabel("Count")
            ax.set_yscale("log")
            ax_cumu = ax.twinx()
            pd.DataFrame.hist(ranking_dataframe, column=rank_method, bins=100, alpha=0.5, ax=ax)
            rank_series = ranking_dataframe[rank_method].copy().sort_values(ascending=False)
            rank_series.hist(
                bins=100, density=True, ax=ax_cumu,
                cumulative=True, histtype='step', alpha=0.4, color='r',
            )
            _fix_hist_step_vertical_line_at_end(ax_cumu)
            ax.set_title(f"# of molecules: {len(ranking_dataframe)}")
            hline_value = np.percentile(rank_series, 100 - top_percentile)
            ax_cumu.axhline(
                y=1 - top_percent,
                color='k',
                label='TOP {:.1f}%\nvalue: {:.2f}\n# of ligands: {}'.format(
                    top_percentile,
                    hline_value,
                    len([v for v in rank_series if v > hline_value])
                )
            )
            ax_cumu.legend()
            fig.savefig(f"{self.ranking_df_dir}/{rank_method}_dist.png", dpi=600)

    @log_time
    def suggestions(self):
        createdir(self.suggestion_dir)

        from lsal.tasks.suggestor import DiversitySuggestor
        ranking_dataframe = pd.read_csv(self.ranking_dataframe_csv)

        taught_ligands = ranking_dataframe[ranking_dataframe['is_taught'] == True]['ligand_identifier'].tolist()
        include_taught = False

        main_pool = {lig.identifier: lig for lig in json_load(self.prediction_ligand_pool_json, gz=True)}
        suggestion_pool = dict()
        ranking_records = []
        for r in ranking_dataframe.to_dict(orient='records'):
            lig_id = r['ligand_identifier']
            if lig_id in suggestion_pool:
                continue
            if lig_id in taught_ligands and not include_taught:
                continue
            suggestion_pool[lig_id] = main_pool[lig_id]
            ranking_records.append(r)

        ranking_dataframe = pd.DataFrame.from_records(ranking_records)

        for rank_method, diversity, percentile_from in [
            # ('rank_average_pred_mu_top2%mu', 'chemistry', 'top'),
            # ('rank_average_pred_mu_top2%mu', 'chemistry', 'bottom'),
            ('rank_average_pred_mu_top2%mu', 'feature', 'top'),
            ('rank_average_pred_mu_top2%mu', 'feature', 'bottom'),

            # ('rank_average_pred_std', 'chemistry', 'top'),
            ('rank_average_pred_std', 'feature', 'top'),

            # ('rank_average_pred_std_top2%mu', 'chemistry', 'top'),
            ('rank_average_pred_std_top2%mu', 'feature', 'top'),
        ]:
            rank_method_short_name = rank_method.replace("rank_average_pred_", "")
            suggestor_name = f"{rank_method_short_name}__{diversity}__{percentile_from}"
            ds = DiversitySuggestor(
                suggestor_name=suggestor_name,
                pool=suggestion_pool,
                ranking_dataframe=ranking_dataframe,
                percentile=2,
                percentile_from=percentile_from,
                batch_size=8,
                diversity_space=diversity,
                ranking_parameter=rank_method,
            )
            logfile = f'{self.suggestion_dir}/suggestion__{ds.suggestor_name}.log'
            csv_file = f'{self.suggestion_dir}/suggestion__{ds.suggestor_name}.csv'
            sink_id = logger.add(logfile)
            cluster_dfs = ds.suggest()
            logger.warning(ds.details)
            logger.remove(sink_id)

            readable_records = []
            for cdf in cluster_dfs:
                readable_records += cdf.to_dict(orient='records')
                readable_records.append(dict())
            readable_df = pd.DataFrame.from_records(readable_records)
            readable_df.to_csv(csv_file, index=False)
            self.collect_files.append(abspath(csv_file))
            self.collect_files.append(abspath(logfile))

    @staticmethod
    def parse_suggestion_df(suggestion_df: pd.DataFrame) -> dict[str: int]:
        """ obtain smiles -> icluster """
        d = dict()
        ic = 0
        for r in suggestion_df.to_dict(orient='records'):
            if any(pd.isna(v) for v in r.values()):
                ic += 1
                continue
            else:
                d[r['ligand_smiles']] = ic
        return d

# # deprecated in favor of mongodb push
# class VisdataExporter(Worker):
#     def __init__(
#             self,
#             code_dir: FilePath,
#             work_dir: FilePath,
#             ligands_json_gz: FilePath,
#             learning_folders: list[FilePath],
#             reaction_collection_folder: FilePath,
#             fp_type: str = 'ECFP4'
#     ):
#         super().__init__(name=self.__class__.__name__, code_dir=code_dir, work_dir=work_dir)
#         self.fp_type = fp_type
#         self.ligands_json_gz = ligands_json_gz
#         self.reaction_collection_folder = reaction_collection_folder
#         self.learning_folders = learning_folders
#
#         self.data = dict()
#
#     @log_time
#     def load_ligands(self):
#         ligands = json_load(self.ligands_json_gz, gz=True)
#         ligands: list[Molecule]
#         self.data['ligands'] = [lig.as_dict() for lig in ligands]
#
#     @log_time
#     def load_svgs(self):
#         assets = f"{self.work_dir}/assets"
#         createdir(assets)
#         for d in self.data['ligands']:
#             smiles = d['smiles']
#             label = get_molecule_label(d['mol_type'], d['int_label'])
#             draw_svg(smiles, fn=f"{assets}/{label}.svg")
#
#     @log_time
#     def load_reaction_collections(self):
#         for json_gz in sorted(glob.glob(f"{self.reaction_collection_folder}/reaction_collection_*.json.gz")):
#             rcname = get_basename(json_gz)
#             self.data[rcname] = json_load(json_gz, gz=True).as_dict()
#
#     @log_time
#     def load_model_results(self):
#         for lf in self.learning_folders:
#             model_name = "Model:" + get_basename(lf).replace("learning_", "")
#             ranking_df = pd.read_csv(f"{lf}/ranking_df/qr_ranking.csv", low_memory=False)
#             suggestion_df = pd.read_csv(f"{lf}/suggestion/suggestion__mu_top2%mu__feature__top.csv", low_memory=False)
#             self.data[model_name] = {
#                 "ranking_df": ranking_df,
#                 "suggestion_df": suggestion_df,
#             }
#
#     @log_time
#     def calculate_pool_counterfactuals(self):
#         for model_name in self.data:
#             if not model_name.startswith("Model:"):
#                 continue
#             ranking_df = self.data[model_name]['ranking_df']
#             suggestion_df = self.data[model_name]['suggestion_df']
#             base_smi_to_cluster = OneLigandWorker.parse_suggestion_df(suggestion_df)
#             suggestion_df = suggestion_df.dropna(axis=0, how="all", inplace=False)
#
#             label_to_smiles = dict(zip(ranking_df['ligand_label'].tolist(), ranking_df['ligand_smiles'].tolist()))
#             smiles_to_label = {v: k for k, v in label_to_smiles.items()}
#             smiles = ranking_df['ligand_smiles'].tolist()
#
#             rank_method = [c for c in suggestion_df.columns if c.startswith('rank_average')]
#             assert len(rank_method) == 1
#             rank_method = rank_method[0]
#
#             smi_to_rp = dict(zip(ranking_df['ligand_smiles'].tolist(), ranking_df[rank_method].tolist()))
#
#             fps = [stoned.get_fingerprint(smi2mol(smi), self.fp_type) for smi in smiles]
#
#             cf_data = dict()
#             base_smis = suggestion_df['ligand_smiles'].tolist()
#             for base_smi in tqdm(base_smis, desc=f'cf data'):
#                 base_rp = smi_to_rp[base_smi]
#                 fp_base = stoned.get_fingerprint(smi2mol(base_smi), self.fp_type)
#                 sim_array = BulkTanimotoSimilarity(fp_base, fps)
#                 # drp_array = [abs(rp - base_rp) for rp in ranking_df[rank_method]]
#                 sim_cutoff = np.percentile(sim_array, q=98)
#                 # drp_cutoff = np.percentile(drp_array, q=90)
#                 data = []
#                 for j, cf_smi in enumerate(smiles):
#                     cf_rp = smi_to_rp[cf_smi]
#                     drp = cf_rp - base_rp
#                     if sim_array[j] > sim_cutoff:
#                         data.append((cf_smi, sim_array[j], drp, base_rp, cf_rp))
#                     # if abs(drp) >= drp_cutoff or sim_array[j] > sim_cutoff:
#                     #     data.append((cf_smi, sim_array[j], drp))
#                     # data.append((cf_smi, sim_array[j], drp))
#                 cf_data[base_smi] = data
#             data = {
#                 'rank_method': rank_method,
#                 'smiles_list': smiles,
#                 'smiles_to_label': smiles_to_label,
#                 'label_to_smiles': label_to_smiles,
#                 'cf_data': cf_data,
#                 'base_smi_to_cluster': base_smi_to_cluster
#             }
#             self.data[model_name]['cf_pool'] = data
#
#     @log_time
#     def dump(self):
#         json_dump(self.data, f"{self.work_dir}/visdata.json.gz", gz=True)

from __future__ import annotations

import random
from copy import deepcopy
from datetime import datetime
from typing import Union

import numpy as np
import pandas as pd
from loguru import logger
from monty.json import MSONable
from sklearn.ensemble import RandomForestRegressor
from skopt import BayesSearchCV

from lsal.alearn.base import MetaLearner, TeachingRecord, QueryRecord
from lsal.schema import L1XReactionCollection, Molecule
from lsal.tasks import MoleculeSampler
from lsal.twinsk import tune_twin_rf, _default_n_estimator, TwinRegressor
from lsal.utils import FilePath, createdir, pkl_dump, truncate_distribution, upper_confidence_interval, \
    unique_element_to_indices, calculate_distance_matrix


class SingleLigandPrediction(MSONable):

    def __init__(self, ligand: Molecule, amounts: Union[list[float], np.ndarray], prediction_values: np.ndarray):
        """
        data class for the matrix generated by twin reg for one ligand
        represent predictions of a ligand over a range of ligand amounts

        :param ligand:
        :param amounts: a list of conc., each correspond a row in `prediction_values[i]`
        :param prediction_values: a 2d np array,
                `prediction_values[i][j]` represents the prediction made for amounts[i] by the jth predictor
        """
        self.prediction_values = prediction_values
        self.amounts = amounts
        self.ligand = ligand
        assert self.prediction_values.ndim == 2
        assert len(self.amounts) == self.prediction_values.shape[0]

    def overall_uncertainty(self, topfrac: float = None):
        if topfrac is None:
            v = float(np.mean(self.pred_std))
        else:
            v = float(np.mean(self.pred_std_of_mu_top(topfrac)))
        return v

    def pred_mu_top(self, topfrac=0.02):
        top = truncate_distribution(self.pred_mu, "top", topfrac)
        return top

    def pred_std_of_mu_top(self, topfrac=0.02):
        idx = truncate_distribution(self.pred_mu, "top", topfrac, True)
        return self.pred_std[idx]

    @property
    def pred_mu(self) -> np.ndarray:
        return self.prediction_values.mean(axis=1)

    @property
    def pred_std(self) -> np.ndarray:
        return self.prediction_values.std(axis=1)

    @property
    def pred_uci(self) -> np.ndarray:
        return np.apply_along_axis(upper_confidence_interval, 1, self.prediction_values)

    @staticmethod
    def from_stacked_predictions(
            stacked_predictions: np.ndarray, ligand_col: list[Molecule],
            ligand_to_amounts: dict[Molecule, list[float]]
    ) -> list[SingleLigandPrediction]:
        """
        parse predictions (as a 2d array) given by twin estimator

        :param stacked_predictions: 2d array, size of ((num_ligands x num_amounts) x ensemble)
        :param ligand_col: a list of non-unique ligands, size of (num_ligands x num_amounts)
        :param ligand_to_amounts:
        :return:
        """
        ligand_to_indices = unique_element_to_indices(ligand_col)
        ligand_learner_predictions = []
        for ligand, indices in ligand_to_indices.items():
            llp = SingleLigandPrediction(ligand, ligand_to_amounts[ligand], stacked_predictions[indices])
            ligand_learner_predictions.append(llp)
        return ligand_learner_predictions

    @staticmethod
    def calculate_ranking(
            pool: list[Molecule], predictions: list[SingleLigandPrediction]
    ) -> pd.DataFrame:
        lig_to_pred = {p.ligand: p for p in predictions}
        assert all(lig in lig_to_pred.keys() for lig in pool)
        records = []
        ligands = []
        for lig in pool:
            lig: Molecule
            pred: SingleLigandPrediction
            pred = lig_to_pred[lig]
            record = {
                'ligand_label': lig.label,
                'ligand_identifier': lig.identifier,
                'rank_average_pred_mu': float(np.mean(pred.pred_mu)),
                'rank_average_pred_std': float(np.mean(pred.pred_std)),
                'rank_average_pred_uci': float(np.mean(pred.pred_uci)),
                'rank_average_pred_mu_top2%mu': float(np.mean(truncate_distribution(pred.pred_mu, "top", 0.02, False))),
                'rank_average_pred_uci_top2%uci': float(
                    np.mean(truncate_distribution(pred.pred_uci, "top", 0.02, False))),
                'rank_average_pred_std_top2%mu': float(
                    np.mean(pred.pred_std[truncate_distribution(pred.pred_mu, "top", 0.02, True)])),
            }
            records.append(record)
            ligands.append(lig)
        df = pd.DataFrame.from_records(records)
        # random sampling
        random_idx_list = list(range(df.shape[0]))
        random.Random(42).shuffle(random_idx_list)
        df['rank_random_index'] = random_idx_list
        # ks sampling in the feature space
        _, feature_mat = Molecule.l1_input(ligands, None)
        dmat_feature = calculate_distance_matrix(feature_mat)
        ms = MoleculeSampler(ligands, dmat_feature)
        ks_indices = ms.sample_ks(return_mol=False)
        df['rank_ks_feature'] = ks_indices
        # TODO ks sampling with fp distances
        return df

    @staticmethod
    def query(
            pool: list[Molecule],
            ranking_df: pd.DataFrame,
            model_path: FilePath,
            size: int = None, ) -> QueryRecord:
        if size is None:
            size = len(pool)
        query_results = dict()
        for col in ranking_df.columns:
            if col.startswith('rank_'):
                query_results[col] = ranking_df.nlargest(size, col, keep='first')['ligand_label'].tolist()
        qr = QueryRecord(datetime.now(), model_path, ranking_df, query_results)
        return qr


class TeachingRecordL1(TeachingRecord):
    def __init__(
            self, date: datetime, model_path: FilePath,
            X, y,
            init_base_estimator_params,
            final_base_estimator_params,
            reaction_collection: L1XReactionCollection,
            ligand_column: list[Molecule],
            tuning: dict,
    ):
        super().__init__(date, model_path, X, y)
        self.tuning = tuning
        self.ligand_column = ligand_column
        self.reaction_collection = reaction_collection
        self.final_base_estimator_params = final_base_estimator_params
        self.init_base_estimator_params = init_base_estimator_params


class SingleLigandLearner(MetaLearner):
    """
    active learner for single ligand reactions
    """

    @property
    def latest_teaching_record(self) -> TeachingRecordL1:
        assert len(self.teaching_records) > 0, "no teaching record"
        return self.teaching_records[-1]

    @classmethod
    def init_trfr(
            cls,
            teaching_figure_of_merit: str,
            wdir: FilePath,
    ):
        """
        use this to construct from a model object
        """
        createdir(wdir)
        model = TwinRegressor(
            RandomForestRegressor(
                n_estimators=_default_n_estimator,
                random_state=42, n_jobs=-1
            )
        )
        mpath = f'{wdir}/model_init.pkl'
        pkl_dump(model, mpath)
        init_tr = TeachingRecordL1(
            date=datetime.now(),
            model_path=mpath,
            X=pd.DataFrame(),
            y=pd.DataFrame(),
            init_base_estimator_params=model.twin_base_estimator.get_params(),
            final_base_estimator_params=model.twin_base_estimator.get_params(),
            reaction_collection=L1XReactionCollection([]),
            ligand_column=[],
            tuning={}
        )
        learner = cls(
            work_dir=wdir,
            teaching_figure_of_merit=teaching_figure_of_merit,
            teaching_records=[init_tr, ],
        )
        learner.current_model = model
        return learner

    def teach_reactions(
            self, reaction_collection: L1XReactionCollection,
            model_path: FilePath,
            tune=False, split_in_tune=True
    ) -> TeachingRecordL1:
        ligands, X, y = reaction_collection.l1_input(fom_def=self.teaching_figure_of_merit)
        logger.info("teaching with dataframe of size: {}".format(X.shape))
        assert self.current_model is not None and len(self.teaching_records) > 0
        assert model_path not in [tr.model_path for tr in self.teaching_records]
        assert isinstance(self.current_model, TwinRegressor)
        init_params = self.current_model.twin_base_estimator.get_params()

        if tune:
            X_train, y_train, X_test, y_test, opt = tune_twin_rf(X, y, use_split=split_in_tune)
            opt: BayesSearchCV
            tuning_results = {
                "X_train": X_train, "y_train": y_train, "X_test": X_test, "y_test": y_test,
                "opt_results": opt.optimizer_results_,
                "opt_cv_results": opt.cv_results_, "opt_params": opt.best_params_
            }
            self.current_model = opt.best_estimator_
            final_params = self.current_model.twin_base_estimator.get_params()
        else:
            tuning_results = dict()
            final_params = deepcopy(init_params)
        self.current_model.fit(X.values, y.values)
        pkl_dump(self.current_model, model_path)
        tr = TeachingRecordL1(
            date=datetime.now(),
            model_path=model_path,
            X=X,
            y=y,
            init_base_estimator_params=init_params,
            final_base_estimator_params=final_params,
            reaction_collection=reaction_collection,
            ligand_column=ligands,
            tuning={
                'tuning_results': tuning_results,
                'tune': tune,
                'split_in_tune': split_in_tune,
            }
        )
        self.teaching_records.append(tr)
        return tr

    def predict(
            self, ligands: list[Molecule], amounts: Union[list[float], np.ndarray]
    ) -> list[SingleLigandPrediction]:
        assert self.current_model is not None
        assert len(self.latest_teaching_record.reaction_collection) > 0
        for lig in ligands:
            if lig in self.latest_teaching_record.reaction_collection.unique_ligands:
                logger.warning(f"making predictions for an already taught ligand: {lig}")
        ligand_col, df_x = Molecule.l1_input(ligands, amounts)
        ligand_to_amounts = {lig: amounts for lig in ligands}
        stacked_predictions = self.current_model.twin_predict_distribution(df_x.values)
        llps = SingleLigandPrediction.from_stacked_predictions(stacked_predictions, ligand_col, ligand_to_amounts)
        return llps

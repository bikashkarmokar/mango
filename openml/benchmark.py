import json
import warnings
from collections import namedtuple
import os
import random
import re

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from xgboost import XGBClassifier
from sklearn.svm import SVC
from sklearn.model_selection import cross_val_score
from scipy.stats import uniform
from joblib import Parallel, delayed
import pandas as pd
from hyperopt import fmin, tpe, hp, STATUS_OK, Trials, STATUS_FAIL
from hyperopt.pyll import scope
from hyperopt.mongoexp import MongoTrials


from mango.domain.distribution import loguniform
from mango.tuner import Tuner

warnings.simplefilter(action='ignore', category=FutureWarning)

_svm_taskids = [10101, 145878, 146064, 14951, 34536, 34537, 3485,
                3492, 3493, 3494, 37, 3889, 3891, 3899, 3902, 3903,
                3913, 3918, 3950, 6566, 9889, 9914, 9946, 9952, 9967,
                9971, 9976, 9978, 9980, 9983]

_xgb_taskids = [10093, 10101, 125923, 145847, 145857, 145862, 145872,
                145878, 145953, 145972, 145976, 145979, 146064, 14951,
                31, 3485, 3492, 3493, 37, 3896, 3903, 3913, 3917, 3918,
                3, 49, 9914, 9946, 9952, 9967]

_rf_taskids = [125923, 145804, 145836, 145839, 145855, 145862, 145878,
               145972, 145976, 146065, 31, 3492, 3493, 37, 3896, 3902,
               3913, 3917, 3918, 3950, 3, 49, 9914, 9952, 9957, 9967,
               9970, 9971, 9978, 9983]

_bad_tasks = [6566, 34536, 3950]  # no features (3950, 10101 takes too much time)

_data_dir = "data"
_results_dir = "results3"


@scope.define
def hp_int(a):
    return int(a)


def hp_range(label, low, high):
    return scope.hp_int(hp.quniform(label, low, high, 1))


class RandomForest(RandomForestClassifier):

    @classmethod
    def mango_space(cls):
        """
         mtry in paper can't use 1 to 36 as it will error if max_features < 36
         using fractions give the same effect of going from small to max
         sklearn does not implement sample fraction
         adding these additional params not in paper
        """
        return {
            'max_features': ['sqrt', 'log2', 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
            'n_estimators': range(1, 2000),
            'bootstrap': [True, False],  # sample with replacement
            'max_depth': range(1, 20),
            'min_samples_leaf': range(1, 10)
        }

    @classmethod
    def hp_space(cls):
        ms = cls.mango_space()
        return {
            'max_features': hp.choice('max_features', ms['max_features']),
            'n_estimators': hp_range('n_estimators', 1, 2000),
            'bootstrap': hp.choice('bootstrap', ms['bootstrap']),
            'max_depth': hp_range('max_depth', 1, 20),
            'min_samples_leaf': hp_range('min_samples_leaf', 1, 10),
        }


class SVM(SVC):

    @classmethod
    def mango_space(cls):
        return {
            'C': loguniform(-3, 6),  # 10^-3 to 10^3
            'kernel': ['rbf', 'sigmoid'],
            'gamma': loguniform(-3, 6),  # 10^-3 to 10^3
            # 'degree': range(2, 6),
        }

    @classmethod
    def hp_space(cls):
        return {
            'C': hp.loguniform('C', np.log(10 ** -3), np.log(10 ** 3)),  # 10^-3 to 10^3
            'kernel': hp.choice('kernel', ['rbf', 'sigmoid']),
            'gamma': hp.loguniform('gamma', np.log(10 ** -3), np.log(10 ** 3)),  # 10^-3 to 10^3
            # 'degree': hp_range('degree', 2, 5),
        }


class XGB(XGBClassifier):

    @classmethod
    def mango_space(cls):
        return {
            'n_estimators': range(3, 5000),
            'max_depth': range(1, 15),
            'reg_alpha': loguniform(-3, 6),  # 10^-3 to 10^3
            # 'booster': ['gbtree', 'gblinear'],
            'colsample_bylevel': uniform(0.05, 0.95),
            'colsample_bytree': uniform(0.05, 0.95),
            'learning_rate': loguniform(-3, 3),  # 0.001 to 1
            'reg_lambda': loguniform(-3, 6),  # 10^-3 to 10^3
            'min_child_weight': loguniform(0, 2),
            'subsample': uniform(0.1, 0.89),
        }

    @classmethod
    def hp_space(cls):
        return {
            'n_estimators': hp_range('n_estimators', 3, 5000),
            'max_depth': hp_range('max_depth', 1, 15),
            'reg_alpha': hp.loguniform('reg_alpha', np.log(10 ** -3), np.log(10 ** 3)),  # 10^-3 to 10^3
            # 'booster': hp.choice('booster', ['gbtree', 'gblinear']),
            'colsample_bylevel': hp.uniform('colsample_bylevel', 0.05, 0.99),
            'colsample_bytree': hp.uniform('colsample_bytree', 0.05, 0.99),
            'learning_rate': hp.loguniform('learning_rate', np.log(10 ** -3), np.log(1)),  # 0.001 to 1
            'reg_lambda': hp.loguniform('reg_lambda', np.log(10 ** -3), np.log(10 ** 3)),  # 10^-3 to 10^3
            'min_child_weight': hp.loguniform('min_child_weight', 0, np.log(10 ** 2)),
            'subsample': hp.uniform('subsample', 0.1, 0.99),
        }


_constructors = dict(rf=RandomForest, svm=SVM, xgb=XGB)
_task_ids = dict(rf=_rf_taskids, svm=_svm_taskids, xgb=_xgb_taskids)

OptimizationTask = namedtuple('OptimizationTask', 'id scorer mango_space hp_space')


def optimization_tasks(clf_id):
    res = []

    for task_id in _task_ids[clf_id]:
        if task_id in _bad_tasks:
            continue
        res.append(OptimizationTask(
            id=f'{clf_id}-{task_id}',
            scorer=get_scorer(clf_id, task_id),
            mango_space=_constructors[clf_id].mango_space(),
            hp_space=_constructors[clf_id].hp_space())
        )

    return res


def get_scorer(clf_id, task_id, cv=3, scoring='roc_auc'):
    X, y = load_data(task_id)

    def scorer(params):
        clf = _constructors[clf_id](**params)
        return np.mean(cross_val_score(clf, X, y, cv=cv, scoring=scoring))

    return scorer


def load_data(task_id):
    df = pd.read_csv(f'{_data_dir}/{task_id}.csv')
    with open(f'{_data_dir}/{task_id}.json', 'r') as f:
        meta = json.load(f)

    y_col = next(x['name'] for x in meta['features'] if x['target'] == '1')
    x_cols = [col for col in df.columns if col != y_col]

    X = df[x_cols]
    X = pd.get_dummies(X)

    y = df[y_col]
    classes = list(y.unique())
    y = y.apply(classes.index)

    return X, y


class Benchmark:

    def __init__(self, max_evals, n_parallel, n_repeat):
        self.max_evals = max_evals
        self.task = None
        self.n_parallel = n_parallel
        self.n_repeat = n_repeat

    @property
    def hp_objective(self):
        def objective(params):
            loss = - self.task.scorer(params)
            return {
                'loss': loss,
                'status': STATUS_OK
            }

        return objective

    @property
    def mango_objective(self):

        def objective(params_list):
            scores = []
            params_evaluated = []
            for params in params_list:
                score = self.task.scorer(params)
                scores.append(score)
                params_evaluated.append(params)

            return params_evaluated, scores

        return objective

    @property
    def mango_parallel_objective(self):

        def objective(params_list):
            scores = Parallel(n_jobs=len(params_list))(
                delayed(self.task.scorer)(params) for params in params_list)

            return params_list, scores

        return objective

    def hp_serial(self):
        trials = Trials()
        batch_size = 1
        best_params = fmin(
            fn=self.hp_objective,
            space=self.task.hp_space,
            algo=tpe.suggest,
            max_evals=self.max_evals,
            trials=trials
        )
        scores = [-t['result']['loss'] for t in trials.trials]
        print("hp serial task: %s, best: %s, params: %s" %
              (self.task.id, max(scores), best_params))

        search_path = trials.vals
        search_path['score'] = list(np.array(trials.losses()) * -1)

        return self.accumulate_max(scores, self.max_evals, batch_size), search_path

    def hp_parallel(self):
        trials = MongoTrials('mongo://localhost:27017/foo_db/jobs',
                             exp_key=self.task.id + str(random.getrandbits(64)))
        batch_size = self.n_parallel
        best_params = fmin(
            fn=self.hp_objective,
            space=self.task.hp_space,
            algo=tpe.suggest,
            max_evals=self.max_evals * batch_size,
            trials=trials
        )
        scores = [-t['result']['loss'] for t in trials.trials]
        print("hp parallel task: %s, best: %s, params: %s" %
              (self.task.id, max(scores), best_params))

        search_path = trials.vals
        search_path['score'] = list(np.array(trials.losses()) * -1)

        return self.accumulate_max(scores, self.max_evals, batch_size), search_path

    def mango_serial(self):
        batch_size = 1
        tuner = Tuner(self.task.mango_space,
                      self.mango_objective,
                      dict(num_iteration=self.max_evals,
                           batch_size=batch_size,
                           domain_size=50000))
        results = tuner.maximize()

        print("mango serial task: %s, best: %s, params: %s" %
              (self.task.id,
               results['best_objective'],
               results['best_params']))

        scores = results['objective_values']

        search_path = list(results['params_tried'])
        for idx, sp in enumerate(search_path):
            sp['score'] = results['objective_values'][idx]

        return self.accumulate_max(scores[-self.max_evals * batch_size:], self.max_evals, batch_size), search_path

    @staticmethod
    def accumulate_max(arr, n_iterations, batch_size):
        assert len(arr) == n_iterations * batch_size
        return [max(arr[:(i + 1) * batch_size]) for i in range(n_iterations)]

    def mango_parallel(self):
        batch_size = self.n_parallel
        tuner = Tuner(self.task.mango_space,
                      self.mango_parallel_objective,
                      dict(num_iteration=self.max_evals,
                           batch_size=batch_size))
        results = tuner.maximize()

        print("mango parallel task: %s, best: %s, params: %s" %
              (self.task.id,
               results['best_objective'],
               results['best_params']))

        scores = results['objective_values']

        search_path = list(results['params_tried'])
        for idx, sp in enumerate(search_path):
            sp['score'] = results['objective_values'][idx]

        return self.accumulate_max(scores[-self.max_evals * batch_size:], self.max_evals, batch_size), search_path

    def mango_parallel_cluster(self):
        batch_size = self.n_parallel
        tuner = Tuner(self.task.mango_space,
                      self.mango_parallel_objective,
                      dict(num_iteration=self.max_evals,
                           batch_size=batch_size,
                           parallel_strategy='clustering'))
        results = tuner.maximize()

        print("mango parallel cluster task: %s, best: %s, params: %s" %
              (self.task.id,
               results['best_objective'],
               results['best_params']))

        scores = results['objective_values']
        search_path = list(results['params_tried'])
        for idx, sp in enumerate(search_path):
            sp['score'] = results['objective_values'][idx]
            sp['surrogate'] = results['surrogate_values'][idx]

        return self.accumulate_max(scores[-self.max_evals * batch_size:], self.max_evals, batch_size), search_path

    def random_serial(self):
        batch_size = 1
        tuner = Tuner(self.task.mango_space,
                      self.mango_objective,
                      dict(num_iteration=self.max_evals,
                           batch_size=batch_size,
                           optimizer='Random'))
        results = tuner.maximize()

        print("random task: %s, best: %s, params: %s" %
              (self.task.id,
               results['best_objective'],
               results['best_params']))

        scores = results['objective_values']
        search_path = list(results['params_tried'])
        for idx, sp in enumerate(search_path):
            sp['score'] = results['objective_values'][idx]

        return self.accumulate_max(scores[-self.max_evals * batch_size:], self.max_evals, batch_size), search_path

    def run(self, optimization_task, optimizer, refresh=False):
        result_file = self.result_file(optimization_task.id, optimizer)
        if not refresh and os.path.exists(result_file):
            with open(result_file, 'r') as f:
                res = json.load(f)
            if res['max_evals'] == self.max_evals and \
                    res['batch_size'] == self.n_parallel: # and \
                    # res['n_repeat'] == self.n_repeat:
                print("%s already exists" % optimization_task.id)
                return res

        self.task = optimization_task
        print("Benchmark %s on %s, max evals: %s, batch_size: %s, n_repeat: %s" %
              (optimizer, self.task.id, self.max_evals, self.n_parallel, self.n_repeat))

        res = dict(task_id=self.task.id,
                   max_evals=self.max_evals,
                   batch_size=self.n_parallel,
                   n_repeat=self.n_repeat)

        res['experiments'] = []
        scores = []
        for i in range(self.n_repeat):
            score, search_path = getattr(self, optimizer)()
            scores.append(score)
            res['experiments'].append(search_path)

        res['scores'] = list(np.mean(scores, axis=0))

        if not os.path.exists(os.path.dirname(result_file)):
            os.makedirs(os.path.dirname(result_file))

        with open(result_file, 'w') as f:
            json.dump(res, f)

        return res

    @staticmethod
    def result_file(task_id, optimizer):
        return os.path.join(_results_dir, optimizer, task_id + '.json')


if __name__ == "__main__":
    avail_optimizers = ['mango_serial', 'random_serial', 'hp_serial', 'mango_parallel', 'mango_parallel_cluster']
    all_clf_ids = ['rf', 'xgb', 'svm']
    optimizers = os.environ.get("OPTIMIZER", 'mango_serial,hp_serial,random_serial').split(',')
    print(optimizers)
    assert all(optimizer in avail_optimizers for optimizer in optimizers)

    clf_ids = os.environ.get("CLF_IDS")
    if clf_ids:
        clf_ids = clf_ids.split(',')
    else:
        clf_ids = all_clf_ids
    print(clf_ids)

    task_filter = os.environ.get("TASK", 'xgb-146')

    # b = Benchmark(max_evals=5, n_parallel=4, n_repeat=1)
    b = Benchmark(max_evals=50, n_parallel=2, n_repeat=3)
    for clf_id in clf_ids:
        for task in optimization_tasks(clf_id):

            if task_filter and not re.match(task_filter, task.id):
                continue
            for optimizer in optimizers:
                b.run(task, optimizer, refresh=True)



"""

            'n_estimators': range(3, 5000),
            'max_depth': range(1, 15),
            'reg_alpha': loguniform(-3, 6),  # 10^-3 to 10^3
            'booster': ['gbtree', 'gblinear'],
            'colsample_bylevel': uniform(0.05, 0.95),
            'colsample_bytree': uniform(0.05, 0.95),
            'learning_rate': loguniform(-3, 3),  # 0.001 to 1
            'reg_lambda': loguniform(-3, 6),  # 10^-3 to 10^3
            'min_child_weight': loguniform(0, 2),
            'subsample': uniform(0.1, 0.89),
            
Benchmark hp_serial on xgb-146064, max evals: 50, batch_size: 5, n_repeat: 1
hp serial task: xgb-146064, best: 0.9865662211165457, params: {'booster': 0, 'colsample_bylevel': 0.16659793323235653, 'colsample_bytree': 0.6639739912492748, 'learning_rate': 0.20055675364463627, 'max_depth': 2.0, 'min_child_weight': 1.8790966841235113, 'n_estimators': 3180.0, 'reg_alpha': 0.0012038936899647617, 'reg_lambda': 0.0018377165958774174, 'subsample': 0.6613420064261579}

Benchmark random_serial on xgb-146064, max evals: 50, batch_size: 5, n_repeat: 1
random task: xgb-146064, best: 0.7311772853769607, params: {'booster': 'gbtree', 'colsample_bylevel': 0.959399345514509, 'colsample_bytree': 0.9498495624669465, 'learning_rate': 0.0014002953931664818, 'max_depth': 13, 'min_child_weight': 8.963225235753109, 'n_estimators': 3016, 'reg_alpha': 0.004629184070684072, 'reg_lambda': 0.634735819701405, 'subsample': 0.7993237016254977}
random task: xgb-146064, best: 0.8389869497385731, params: {'booster': 'gbtree', 'colsample_bylevel': 0.27132408394037894, 'colsample_bytree': 0.6201623160391286, 'learning_rate': 0.07129365629279757, 'max_depth': 6, 'min_child_weight': 1.8360956352383773, 'n_estimators': 566, 'reg_alpha': 0.0025974628039601906, 'reg_lambda': 1.9431125528549607, 'subsample': 0.7776543717690461}

mango serial task: xgb-146064, best: 0.7854548616967449, params: {'booster': 'gbtree', 'colsample_bylevel': 0.5177913190802533, 'colsample_bytree': 0.7134898210079639, 'learning_rate': 0.45782989090666154, 'max_depth': 14, 'min_child_weight': 1.0386523391167433, 'n_estimators': 494, 'reg_alpha': 2.2436052149354104, 'reg_lambda': 110.91369625161579, 'subsample': 0.6860898048969027}

"""
"""Native XGBoost 3-class model and disjoint sigmoid/logistic calibration.

Models use portable UBJSON and JSON calibration parameters, never pickle.
Heavy dependencies are loaded only by the offline research process.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .features import FEATURE_NAMES


@dataclass
class ResearchModel:
    booster: object
    coefficients: object
    intercept: object
    classes: object
    calibration_counts: list

    def raw(self, examples):
        import numpy as np
        import xgboost as xgb

        matrix = xgb.DMatrix(
            np.asarray([e.features for e in examples], dtype=float),
            feature_names=list(FEATURE_NAMES),
            nthread=1,
        )
        return self.booster.predict(matrix)

    def predict(self, examples):
        import numpy as np

        raw = self.raw(examples)
        decision = np.log(np.clip(raw, 1e-9, 1)) @ np.asarray(
            self.coefficients
        ).T + np.asarray(self.intercept)
        if decision.shape[1] == 1:
            positive = 1 / (1 + np.exp(-np.clip(decision[:, 0], -40, 40)))
            probabilities = np.column_stack((1 - positive, positive))
        else:
            decision -= decision.max(axis=1, keepdims=True)
            exp = np.exp(decision)
            probabilities = exp / exp.sum(axis=1, keepdims=True)
        result = np.zeros((len(examples), 3))
        for i, label in enumerate(self.classes):
            result[:, int(label)] = probabilities[:, i]
        return result

    def save(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=False)
        self.booster.save_model(path / "model.ubj")
        data = dict(
            coefficients=self.coefficients,
            intercept=self.intercept,
            classes=self.classes,
            calibration_counts=self.calibration_counts,
            feature_names=list(FEATURE_NAMES),
        )
        (path / "calibration.json").write_text(
            json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path):
        import xgboost as xgb

        path = Path(path)
        data = json.loads((path / "calibration.json").read_text(encoding="utf-8"))
        if data["feature_names"] != list(FEATURE_NAMES):
            raise ValueError("Feature schema mismatch")
        booster = xgb.Booster(params={"nthread": 1})
        booster.load_model(path / "model.ubj")
        return cls(
            booster,
            data["coefficients"],
            data["intercept"],
            data["classes"],
            data["calibration_counts"],
        )


def fit(train, calibration, config):
    import numpy as np
    import xgboost as xgb
    from sklearn.linear_model import LogisticRegression

    if (
        not train
        or not calibration
        or {e.label for e in train} == {1}
        or len({e.label for e in calibration}) < 2
    ):
        raise ValueError("Insufficient class diversity for fitting/calibration")
    matrix = xgb.DMatrix(
        np.asarray([e.features for e in train], dtype=float),
        label=[e.label for e in train],
        feature_names=list(FEATURE_NAMES),
        nthread=1,
    )
    params = dict(
        objective="multi:softprob",
        num_class=3,
        eval_metric="mlogloss",
        tree_method="hist",
        max_depth=3,
        eta=0.05,
        min_child_weight=10,
        reg_lambda=10,
        reg_alpha=1,
        subsample=1,
        colsample_bytree=1,
        nthread=1,
        seed=config.seed,
        device="cpu",
    )
    booster = xgb.train(params, matrix, num_boost_round=config.trees)
    model = ResearchModel(
        booster,
        None,
        None,
        None,
        [sum(e.label == k for e in calibration) for k in range(3)],
    )
    raw = model.raw(calibration)
    calibrator = LogisticRegression(
        C=1, max_iter=1000, solver="lbfgs", random_state=config.seed
    )
    calibrator.fit(
        np.log(np.clip(raw, 1e-9, 1)), np.asarray([e.label for e in calibration])
    )
    model.coefficients = calibrator.coef_.tolist()
    model.intercept = calibrator.intercept_.tolist()
    model.classes = calibrator.classes_.tolist()
    return model

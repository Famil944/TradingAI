"""Time-cohort splits with label purging across every boundary."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ResearchConfig:
    train_times: int = 2000
    calibration_times: int = 500
    validation_times: int = 500
    test_times: int = 500
    holdout_fraction: float = 0.2
    min_folds: int = 3
    min_trades: int = 100
    min_days: int = 20
    min_class_count: int = 20
    probability_floor: float = 0.60
    min_expected_return: float = 0.05
    max_ece: float = 0.08
    cooldown: int = 900
    bootstrap_replicates: int = 1000
    trees: int = 100
    seed: int = 17

    def validate(self):
        integers = (
            self.train_times,
            self.calibration_times,
            self.validation_times,
            self.test_times,
            self.min_folds,
            self.min_trades,
            self.min_days,
            self.min_class_count,
            self.bootstrap_replicates,
            self.trees,
        )
        if (
            any(not isinstance(v, int) or v < 1 for v in integers)
            or not 0 < self.holdout_fraction < 0.5
        ):
            raise ValueError("Invalid research configuration")
        if (
            not 0.5 <= self.probability_floor < 1
            or not 0 <= self.min_expected_return <= 1
            or not 0 < self.max_ece < 1
            or self.cooldown < 0
        ):
            raise ValueError("Invalid signal criteria")

    def to_dict(self):
        return asdict(self)


def cohort(examples, start, end):
    # Label availability, not just the decision timestamp, controls admission.
    return [e for e in examples if start <= e.at < end and e.label_available_at < end]


def partitions(examples, config):
    config.validate()
    times = sorted({e.at for e in examples})
    holdout_index = int(len(times) * (1 - config.holdout_fraction))
    minimum = (
        config.train_times
        + config.calibration_times
        + config.validation_times
        + config.test_times
    )
    if holdout_index < minimum or holdout_index >= len(times):
        return [], None
    holdout_start = times[holdout_index]
    end = times[-1] + 1

    def split(train_stop, test_stop):
        cal = times[train_stop]
        validation = times[train_stop + config.calibration_times]
        test = times[train_stop + config.calibration_times + config.validation_times]
        return dict(
            train=cohort(examples, times[0], cal),
            calibration=cohort(examples, cal, validation),
            validation=cohort(examples, validation, test),
            test=cohort(examples, test, test_stop),
            boundaries=dict(
                calibration_from=cal,
                validation_from=validation,
                test_from=test,
                test_to=test_stop,
            ),
        )

    folds = []
    train_stop = config.train_times
    while (
        train_stop
        + config.calibration_times
        + config.validation_times
        + config.test_times
        <= holdout_index
    ):
        stop = (
            train_stop
            + config.calibration_times
            + config.validation_times
            + config.test_times
        )
        folds.append(split(train_stop, times[stop] if stop < len(times) else end))
        train_stop += config.test_times
    final_train = holdout_index - config.calibration_times - config.validation_times
    final = split(final_train, end)
    assert final["boundaries"]["test_from"] == holdout_start
    return folds, final

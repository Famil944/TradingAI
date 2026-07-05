class VolumeAnalyzer:

    def analyze(self, analysis: dict):
        current = analysis.get("volume", 0)
        average = analysis.get("volume_avg", current)

        if average <= 0:
            return {"level": "UNKNOWN", "score": 0}

        ratio = current / average

        if ratio >= 2:
            return {"level": "VERY_HIGH", "score": 100}

        if ratio >= 1.5:
            return {"level": "HIGH", "score": 80}

        if ratio >= 1:
            return {"level": "NORMAL", "score": 60}

        if ratio >= 0.7:
            return {"level": "LOW", "score": 30}

        return {"level": "VERY_LOW", "score": 10}
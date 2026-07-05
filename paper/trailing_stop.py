class TrailingStop:

    def __init__(self, trigger_percent=0.5, distance_percent=0.3):
        self.trigger_percent = trigger_percent
        self.distance_percent = distance_percent

    def update(self, position, current_price):
        if position.side == "LONG":
            profit_percent = (
                (current_price - position.entry_price)
                / position.entry_price
            ) * 100

            if profit_percent >= self.trigger_percent:
                new_stop = current_price * (
                    1 - self.distance_percent / 100
                )

                if (
                    position.stop_loss is None
                    or new_stop > position.stop_loss
                ):
                    position.update_stop_loss(round(new_stop, 4))
                    return True

        elif position.side == "SHORT":
            profit_percent = (
                (position.entry_price - current_price)
                / position.entry_price
            ) * 100

            if profit_percent >= self.trigger_percent:
                new_stop = current_price * (
                    1 + self.distance_percent / 100
                )

                if (
                    position.stop_loss is None
                    or new_stop < position.stop_loss
                ):
                    position.update_stop_loss(round(new_stop, 4))
                    return True

        return False
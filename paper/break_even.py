class BreakEven:

    def __init__(self, offset_percent=0.05):
        self.offset_percent = offset_percent

    def move_to_break_even(self, position):
        if position.side == "LONG":
            new_stop = position.entry_price * (
                1 + self.offset_percent / 100
            )

            if position.stop_loss is None or new_stop > position.stop_loss:
                position.update_stop_loss(round(new_stop, 4))
                return True

        if position.side == "SHORT":
            new_stop = position.entry_price * (
                1 - self.offset_percent / 100
            )

            if position.stop_loss is None or new_stop < position.stop_loss:
                position.update_stop_loss(round(new_stop, 4))
                return True

        return False
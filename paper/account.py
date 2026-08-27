class PaperAccount:

    def __init__(self, balance=10000):
        self.balance = balance

    def get_balance(self):
        return round(self.balance, 2)

    def deposit(self, amount):
        self.balance += amount

    def withdraw(self, amount):
        if amount > self.balance:
            return False

        self.balance -= amount
        return True
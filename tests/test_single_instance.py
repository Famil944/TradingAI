import unittest

from core.single_instance import SingleInstance


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_cannot_acquire_same_port(self):
        first = SingleInstance(port=48761)
        second = SingleInstance(port=48761)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            second.close()
            first.close()


if __name__ == "__main__":
    unittest.main()

import unittest

import bazel.python.test_wheel_lib as test_wheel_lib


class TestWheelInstall(unittest.TestCase):
    def test_helloReturnsExpectedString(self):
        self.assertEqual(test_wheel_lib.hello(), "hello from test_wheel")


if __name__ == "__main__":
    unittest.main()

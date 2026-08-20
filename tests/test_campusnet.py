import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import campusnet


class SRunEncodingTests(unittest.TestCase):
    def test_custom_base64_matches_bit_portal_alphabet(self) -> None:
        self.assertEqual(
            campusnet.srun_base64_encode(b"CampusNet SRun info"),
            "+IifMNuzFUut2i0mnaDSOaeUWv==",
        )

    def test_xencode_and_custom_base64_match_browser_reference(self) -> None:
        payload = '{"username":"test","password":"pass","ip":"10.1.2.3","acid":"8","enc_ver":"srun_bx1"}'
        token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        encoded = campusnet.srun_base64_encode(campusnet.srun_xencode(payload, token))
        self.assertEqual(
            encoded,
            "bBkzWXCkz1O2rY3owbSjTm17PM20o0rVW89iXDg6ZX32BwIwIE5hF7oOkB9mjATYVbCOIx7LDq+5mfdflJa6VgDttR+g3/GYErDMb/bvs1KlGlb1kSuWByxL0s4=",
        )

    def test_invalid_alphabet_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            campusnet.srun_base64_encode(b"data", "A" * 64)


if __name__ == "__main__":
    unittest.main()

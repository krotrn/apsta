# `iw phy <phy> info` fixtures

Real or representative driver output used by the capability parser tests.
Contributions welcome: if `apsta detect` gets your card wrong, add its
`iw phy phy0 info` output here with a test describing the expected result.

| File | Card / driver | Expected |
| --- | --- | --- |
| `intel_alderlake.txt` | Intel AX201-class CNVi, iwlwifi (real output) | AP+STA, same channel; Wi-Fi Direct on a second channel |
| `intel_no_p2p_channel.txt` | the same, with the P2P combination limited to one channel | AP+STA, same channel; no Wi-Fi Direct fallback |
| `ath10k_multichannel.txt` | QCA6174, ath10k | AP+STA, different channels allowed |
| `either_or.txt` | AP and managed share a group limited to 1 | AP only, no AP+STA |
| `no_combinations.txt` | single-interface radio | AP only, no AP+STA |
| `no_ap.txt` | client-only radio | no AP |
| `reg_self_managed.txt` | `iw reg get` on an Intel AX201 (global 00, card self-managed IN) | country IN for phy0 |

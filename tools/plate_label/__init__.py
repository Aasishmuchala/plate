"""PLATE labelling tool: three-key verdict (1 Ship / 2 Fix / 3 Render-it) with reason codes on
plate-vs-candidate pairs, written one JSON line per verdict to calib/labels.jsonl. The labels feed
the P1 calibration (gates/plate_gates/calibration.py: AUC of the gate ensemble against these
verdicts). New code for PLATE, no seed script; the reason-code table is calib/reason_codes.yaml.
"""

__version__ = "0.2.0"

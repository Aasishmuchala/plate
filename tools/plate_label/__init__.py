"""PLATE labelling tool: three-key verdict (Ship / Fix / Render-it) with reason codes on a folder of
images, written one JSON line per verdict to calib/labels.jsonl. The labels feed the P1 calibration
(AUC of the gate ensemble against Aasish's verdicts). New code for PLATE, no seed script; the code
table is fixed by the build plan (T8) so the file is stable from the first label.
"""

__version__ = "0.1.0"

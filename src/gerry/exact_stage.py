"""Isolated SCIP exact stage runner.

SCIP 10 exact models loaded through PySCIPOpt can corrupt the native heap while
Python tears down several models in one process. A stage has no reason to keep
the model alive after serializing its scalar result, so this helper deliberately
uses os._exit() after flushing the result and certificate produced by SCIPsolve.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(model_path: Path, proof_path: Path, result_path: Path) -> None:
    from pyscipopt import Model, SCIP_PARAMSETTING

    exit_code = 0
    try:
        model = Model(f"exact-{model_path.stem}")
        model.freeProb()
        model.enableExactSolving(True)
        model.readProblem(str(model_path))
        model.setParam("display/verblevel", 0)
        # SCIP 10.0.2 can emit an invalid VIPR derivation for aggregated rows
        # (observed as AggrRow_* mismatch). Keeping the original integer model
        # avoids that transformation and produces independently checkable proofs.
        model.setPresolve(SCIP_PARAMSETTING.OFF)
        # Gomory separators in SCIP 10.0.2 currently serialize aggregated rows
        # with binary-double artifacts that viprchk cannot derive exactly.
        # Exact branch-and-bound remains complete without those cuts.
        model.setSeparating(SCIP_PARAMSETTING.OFF)
        proof_logging = "certificate/filename" in model.getParams()
        if proof_logging:
            model.setParam("certificate/filename", str(proof_path))
        model.optimize()
        status = str(model.getStatus()).lower()
        variables = {variable.name: variable for variable in model.getVars()}
        assignment_values = {
            name: model.getVal(variable)
            for name, variable in variables.items()
            if name.startswith("x_") and model.getNSols()
        }
        payload = {
            "status": status,
            "nsols": model.getNSols(),
            "objective": model.getObjVal() if status == "optimal" else None,
            "assignment_values": assignment_values,
            "proof_logging": proof_logging,
        }
        del variables
        model.freeProb()  # finalize the certificate while the SCIP instance is valid
    except Exception as exc:
        exit_code = 2
        payload = {"error": f"{type(exc).__name__}: {exc}"}

    temporary = result_path.with_suffix(result_path.suffix + ".part")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(result_path)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m gerry.exact_stage MODEL.cip PROOF.vipr RESULT.json")
    main(*(Path(value) for value in sys.argv[1:]))

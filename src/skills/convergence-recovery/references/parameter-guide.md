# Parameter Guide

Use this guide as the only source for convergence-recovery parameter choices. Match edits to the recorded evidence class and keep changes bounded.

## Electronic Non-Convergence

Symptoms:

- electronic steps reach `NELM`;
- recorded energy changes oscillate;
- evidence reports SCF instability.

Adjust in priority order:

1. `ALGO = Fast -> Normal -> All`
2. `AMIX = 0.4 -> 0.2 -> 0.1`
3. `BMIX = 1.0 or 3.0 -> 0.001 -> 0.0001`
4. `SIGMA = 0.2 -> 0.05` for metals or `0.01` for semiconductors
5. `NELM = 200 -> 400` only after algorithm and mixing changes

Magnetic systems may need more conservative mixing and explicit `MAGMOM` handling. Preserve a scientifically required spin setup.

## Ionic Non-Convergence

Symptoms:

- ionic steps reach `NSW`;
- force remains above target;
- recorded progress stagnates while forces remain high.

Adjust in priority order:

1. `IBRION = 1 -> 2`
2. for difficult starts, `IBRION = 3`
3. `POTIM = 0.5 -> 0.2 or 0.3`
4. `NSW = 200 -> 400`
5. Relax `EDIFFG` only when the repair request explicitly allows a looser intermediate pre-relaxation.

`POTIM` matters for CG and damped MD; it does not fix every `IBRION=1` issue.

## Attempt Boundary

Choose one evidence-backed correction for the prepared attempt. Do not introduce
additional pre-relaxation submissions or loosen the final scientific targets.
Preserve required DFT+U and vdW corrections in comparable final-energy workflows.

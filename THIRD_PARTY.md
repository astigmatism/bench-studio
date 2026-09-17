# Upstream attribution

Bench Studio's original application code is MIT licensed. Third-party material retains its own license.

| Component | Pin | Use / license |
|---|---|---|
| [BetterBench](https://github.com/GGZ14/BetterBench) | v0.6.0, d00ad5ec8098c06584a88ec3468bacd37d5ed098 | Unmodified `vendor/` engine; retained upstream license. See `upstream-source.json` for canonical provenance. |
| [EvalPlus](https://github.com/evalplus/evalplus) | 0.3.1 | Executable Python evaluation, Apache-2.0; installed from locked package. |
| [HumanEval+](https://github.com/evalplus/humanevalplus_release) | v0.1.10 | Downloaded at build time; source digest in `datasets/coding-manifest.json`. |
| [MultiPL-E](https://github.com/nuprl/MultiPL-E) | 3025a531af7450e7df8b96fe0440e9804480bbad | TypeScript verifier; license copied into execution image. HF dataset revision 28441b6024e71d4a1c1c0f6bf171c935cd5a43f2. |
| [Harbor](https://github.com/harbor-framework/harbor) | package 0.23.0; adapter b07f3bfb2c5730c50119c6c84e0e4d8572d9a7f2 | Terminus 2 agent and environment harness. Adapter templates and license under `third_party/harbor-swebenchpro/`. |
| [SWE-bench Pro](https://github.com/scaleapi/SWE-bench_Pro-os) | ca10a60a5fcae51e6948ffe1485d4153d421e6c5 | Official run scripts/parsers, MIT. HF dataset revision 7ab5114912baf22bb098818e604c02fe7ad2c11f. License retained under `third_party/`. |

Original repository task code, downloaded Docker images, and datasets retain the licenses of their respective authors. Caches and benchmark results are not redistributed in this repository. The local network-isolation and fixed-clock adapters are documented; no claim is made that local subset results are official leaderboard scores.

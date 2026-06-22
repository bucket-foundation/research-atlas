# Cross-field results — checkpoint 2

*Generated from `analysis/crossfield/checkpoint_2.json`. Every number below is read from that file.*

- Tranche: top **5,000** most-cited works / field
- Fields with results: **26** / 26
- Total works loaded: **130,000**
- GPU embed throughput (steady-state): **9.1 docs/s** (SPECTER, AMD RX 7700S / ROCm)
- Window: 2015-01-01 .. 2024-12-31

## Generalization: does SPECTER beat TF-IDF in every field?

**SPECTER beats TF-IDF on MAP in 12 of 26 evaluated fields** (win fraction 0.46; sign-test p = 0.845).

Combined field-level test (one-sample bootstrap on the per-field MAP deltas): mean ΔMAP = **-0.0008** (95% CI [-0.0070, +0.0054], p = 0.804).

Fields where SPECTER did **not** beat TF-IDF: Arts and Humanities, Chemical Engineering, Chemistry, Decision Sciences, Earth and Planetary Sciences, Energy, Immunology and Microbiology, Mathematics, Neuroscience, Pharmacology, Toxicology and Pharmaceutics, Physics and Astronomy, Psychology, Veterinary, Health Professions.

## Per-field results

| field | works | eval q | cite Gini | PR Gini | interdisc | TF-IDF MAP | SPECTER MAP | ΔMAP | rel% | p | win |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| Agricultural and Biological Sciences | 5,000 | 383 | 0.288 | 0.353 | 0.389 | 0.186 | 0.203 | +0.017 | +9.1 | 0.113 | ✓ |
| Arts and Humanities | 5,000 | 173 | 0.358 | 0.282 | 0.377 | 0.229 | 0.199 | -0.030 | -13.0 | 0.061 | ✗ |
| Biochemistry, Genetics and Molecular Biology | 5,000 | 660 | 0.443 | 0.427 | 0.269 | 0.170 | 0.172 | +0.002 | +1.4 | 0.729 | ✓ |
| Business, Management and Accounting | 5,000 | 519 | 0.299 | 0.375 | 0.302 | 0.137 | 0.141 | +0.004 | +2.8 | 0.631 | ✓ |
| Chemical Engineering | 5,000 | 1440 | 0.316 | 0.454 | 0.224 | 0.085 | 0.080 | -0.005 | -6.2 | 0.142 | ✗ |
| Chemistry | 5,000 | 1211 | 0.309 | 0.385 | 0.276 | 0.135 | 0.117 | -0.018 | -13.1 | 0.001 | ✗ |
| Computer Science | 5,000 | 1689 | 0.481 | 0.648 | 0.203 | 0.115 | 0.143 | +0.027 | +23.9 | 0.000 | ✓ |
| Decision Sciences | 5,000 | 325 | 0.523 | 0.389 | 0.349 | 0.157 | 0.146 | -0.011 | -6.8 | 0.305 | ✗ |
| Earth and Planetary Sciences | 5,000 | 778 | 0.318 | 0.405 | 0.243 | 0.194 | 0.179 | -0.014 | -7.5 | 0.042 | ✗ |
| Economics, Econometrics and Finance | 5,000 | 245 | 0.339 | 0.431 | 0.224 | 0.167 | 0.183 | +0.016 | +9.8 | 0.145 | ✓ |
| Energy | 5,000 | 2000 | 0.274 | 0.446 | 0.157 | 0.058 | 0.051 | -0.007 | -11.4 | 0.004 | ✗ |
| Engineering | 5,000 | 1076 | 0.273 | 0.385 | 0.238 | 0.148 | 0.153 | +0.005 | +3.3 | 0.326 | ✓ |
| Environmental Science | 5,000 | 614 | 0.314 | 0.407 | 0.303 | 0.170 | 0.185 | +0.014 | +8.3 | 0.061 | ✓ |
| Immunology and Microbiology | 5,000 | 376 | 0.321 | 0.396 | 0.422 | 0.195 | 0.187 | -0.008 | -4.1 | 0.440 | ✗ |
| Materials Science | 5,000 | 1110 | 0.259 | 0.380 | 0.302 | 0.148 | 0.154 | +0.006 | +3.8 | 0.291 | ✓ |
| Mathematics | 5,000 | 467 | 0.456 | 0.440 | 0.329 | 0.142 | 0.120 | -0.022 | -15.4 | 0.010 | ✗ |
| Medicine | 5,000 | 744 | 0.408 | 0.457 | 0.270 | 0.149 | 0.168 | +0.019 | +12.7 | 0.006 | ✓ |
| Neuroscience | 5,000 | 399 | 0.316 | 0.394 | 0.317 | 0.193 | 0.191 | -0.002 | -0.9 | 0.871 | ✗ |
| Nursing | 5,000 | 209 | 0.319 | 0.328 | 0.292 | 0.200 | 0.212 | +0.012 | +5.9 | 0.424 | ✓ |
| Pharmacology, Toxicology and Pharmaceutics | 5,000 | 636 | 0.243 | 0.356 | 0.271 | 0.141 | 0.101 | -0.040 | -28.5 | 0.000 | ✗ |
| Physics and Astronomy | 5,000 | 1630 | 0.487 | 0.449 | 0.113 | 0.171 | 0.161 | -0.010 | -5.7 | 0.034 | ✗ |
| Psychology | 5,000 | 366 | 0.330 | 0.347 | 0.422 | 0.158 | 0.156 | -0.003 | -1.6 | 0.804 | ✗ |
| Social Sciences | 5,000 | 335 | 0.332 | 0.332 | 0.451 | 0.152 | 0.175 | +0.022 | +14.7 | 0.019 | ✓ |
| Veterinary | 5,000 | 523 | 0.318 | 0.360 | 0.173 | 0.221 | 0.214 | -0.007 | -3.3 | 0.442 | ✗ |
| Dentistry | 5,000 | 454 | 0.287 | 0.359 | 0.178 | 0.178 | 0.195 | +0.018 | +10.0 | 0.052 | ✓ |
| Health Professions | 5,000 | 183 | 0.373 | 0.295 | 0.471 | 0.171 | 0.165 | -0.007 | -3.9 | 0.617 | ✗ |

## Concentration (Gini by field)

- Citation-count Gini range across fields: **[0.243, 0.523]**
- PageRank Gini range across fields: **[0.282, 0.648]**

## Interdisciplinarity (fraction of references crossing field boundaries)

- Range across fields: **[0.113, 0.471]** (mean 0.291)

Measured within the union of all loaded fields: for each field, the fraction of its references (whose target is loaded in *some* field) that point to a target in a **different** top-level field.


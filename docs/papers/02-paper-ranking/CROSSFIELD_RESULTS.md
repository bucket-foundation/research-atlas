# Cross-field results — checkpoint 4

*Generated from `analysis/crossfield/checkpoint_4.json`. Every number below is read from that file.*

- Tranche: top **50,000** most-cited works / field
- Fields with results: **26** / 26
- Total works loaded: **361,800**
- Window: 2015-01-01 .. 2024-12-31

## Generalization: does SPECTER beat TF-IDF in every field?

**SPECTER beats TF-IDF on MAP in 11 of 26 evaluated fields** (win fraction 0.42; sign-test p = 0.557).

Combined field-level test (one-sample bootstrap on the per-field MAP deltas): mean ΔMAP = **-0.0019** (95% CI [-0.0075, +0.0034], p = 0.488).

Fields where SPECTER did **not** beat TF-IDF: Arts and Humanities, Chemical Engineering, Chemistry, Decision Sciences, Earth and Planetary Sciences, Energy, Engineering, Immunology and Microbiology, Materials Science, Mathematics, Pharmacology, Toxicology and Pharmaceutics, Physics and Astronomy, Psychology, Veterinary, Health Professions.

## Per-field results

| field | works | eval q | cite Gini | PR Gini | interdisc | TF-IDF MAP | SPECTER MAP | ΔMAP | rel% | p | win |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| Agricultural and Biological Sciences | 20,000 | 712 | 0.286 | 0.374 | 0.329 | 0.154 | 0.154 | +0.000 | +0.3 | 0.942 | ✓ |
| Arts and Humanities | 20,000 | 484 | 0.361 | 0.300 | 0.297 | 0.204 | 0.190 | -0.014 | -6.7 | 0.169 | ✗ |
| Biochemistry, Genetics and Molecular Biology | 6,800 | 921 | 0.444 | 0.434 | 0.322 | 0.157 | 0.175 | +0.018 | +11.2 | 0.005 | ✓ |
| Business, Management and Accounting | 5,000 | 519 | 0.299 | 0.375 | 0.384 | 0.137 | 0.141 | +0.004 | +2.8 | 0.631 | ✓ |
| Chemical Engineering | 5,000 | 1440 | 0.316 | 0.454 | 0.334 | 0.085 | 0.080 | -0.005 | -6.2 | 0.142 | ✗ |
| Chemistry | 20,000 | 2000 | 0.308 | 0.414 | 0.242 | 0.117 | 0.093 | -0.025 | -21.2 | 0.000 | ✗ |
| Computer Science | 20,000 | 2000 | 0.473 | 0.610 | 0.191 | 0.121 | 0.142 | +0.021 | +17.7 | 0.000 | ✓ |
| Decision Sciences | 20,000 | 1066 | 0.501 | 0.405 | 0.283 | 0.127 | 0.111 | -0.017 | -13.0 | 0.001 | ✗ |
| Earth and Planetary Sciences | 20,000 | 1423 | 0.321 | 0.438 | 0.208 | 0.162 | 0.142 | -0.020 | -12.5 | 0.000 | ✗ |
| Economics, Econometrics and Finance | 20,000 | 796 | 0.378 | 0.466 | 0.204 | 0.135 | 0.137 | +0.002 | +1.7 | 0.737 | ✓ |
| Energy | 20,000 | 2000 | 0.322 | 0.467 | 0.169 | 0.045 | 0.038 | -0.008 | -16.6 | 0.000 | ✗ |
| Engineering | 20,000 | 2000 | 0.287 | 0.415 | 0.232 | 0.100 | 0.097 | -0.003 | -3.4 | 0.253 | ✗ |
| Environmental Science | 20,000 | 1240 | 0.317 | 0.424 | 0.293 | 0.146 | 0.150 | +0.003 | +2.2 | 0.519 | ✓ |
| Immunology and Microbiology | 20,000 | 716 | 0.360 | 0.409 | 0.362 | 0.177 | 0.173 | -0.004 | -2.3 | 0.566 | ✗ |
| Materials Science | 20,000 | 2000 | 0.280 | 0.411 | 0.283 | 0.108 | 0.107 | -0.001 | -0.6 | 0.847 | ✗ |
| Mathematics | 20,000 | 1155 | 0.447 | 0.446 | 0.248 | 0.095 | 0.091 | -0.004 | -4.2 | 0.404 | ✗ |
| Medicine | 20,000 | 1192 | 0.419 | 0.471 | 0.264 | 0.148 | 0.157 | +0.009 | +6.2 | 0.089 | ✓ |
| Neuroscience | 20,000 | 843 | 0.330 | 0.414 | 0.270 | 0.170 | 0.185 | +0.015 | +8.6 | 0.033 | ✓ |
| Nursing | 10,000 | 479 | 0.318 | 0.347 | 0.317 | 0.172 | 0.177 | +0.005 | +2.9 | 0.564 | ✓ |
| Pharmacology, Toxicology and Pharmaceutics | 5,000 | 636 | 0.243 | 0.356 | 0.409 | 0.141 | 0.101 | -0.040 | -28.5 | 0.000 | ✗ |
| Physics and Astronomy | 5,000 | 1630 | 0.487 | 0.449 | 0.170 | 0.171 | 0.161 | -0.010 | -5.7 | 0.034 | ✗ |
| Psychology | 5,000 | 366 | 0.330 | 0.347 | 0.491 | 0.158 | 0.156 | -0.003 | -1.6 | 0.804 | ✗ |
| Social Sciences | 5,000 | 335 | 0.332 | 0.332 | 0.519 | 0.152 | 0.175 | +0.022 | +14.7 | 0.019 | ✓ |
| Veterinary | 5,000 | 523 | 0.318 | 0.360 | 0.244 | 0.221 | 0.214 | -0.007 | -3.3 | 0.442 | ✗ |
| Dentistry | 5,000 | 454 | 0.287 | 0.359 | 0.232 | 0.178 | 0.195 | +0.018 | +10.0 | 0.052 | ✓ |
| Health Professions | 5,000 | 183 | 0.373 | 0.295 | 0.558 | 0.171 | 0.165 | -0.007 | -3.9 | 0.617 | ✗ |

## Concentration (Gini by field)

- Citation-count Gini range across fields: **[0.243, 0.501]**
- PageRank Gini range across fields: **[0.295, 0.610]**

## Interdisciplinarity (fraction of references crossing field boundaries)

- Range across fields: **[0.169, 0.558]** (mean 0.302)

Measured within the union of all loaded fields: for each field, the fraction of its references (whose target is loaded in *some* field) that point to a target in a **different** top-level field.


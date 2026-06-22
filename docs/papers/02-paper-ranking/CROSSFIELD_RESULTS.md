# Cross-field results — checkpoint 1

*Generated from `analysis/crossfield/checkpoint_1.json`. Every number below is read from that file.*

- Tranche: top **3,000** most-cited works / field
- Fields with results: **26** / 26
- Total works loaded: **78,000**
- GPU embed throughput (steady-state): **9.1 docs/s** (SPECTER, AMD RX 7700S / ROCm)
- Window: 2015-01-01 .. 2024-12-31

## Generalization: does SPECTER beat TF-IDF in every field?

**SPECTER beats TF-IDF on MAP in 16 of 26 evaluated fields** (win fraction 0.62; sign-test p = 0.327).

Combined field-level test (one-sample bootstrap on the per-field MAP deltas): mean ΔMAP = **+0.0095** (95% CI [-0.0005, +0.0195], p = 0.0624).

Fields where SPECTER did **not** beat TF-IDF: Arts and Humanities, Chemical Engineering, Chemistry, Earth and Planetary Sciences, Energy, Immunology and Microbiology, Mathematics, Pharmacology, Toxicology and Pharmaceutics, Physics and Astronomy, Health Professions.

## Per-field results

| field | works | eval q | cite Gini | PR Gini | interdisc | TF-IDF MAP | SPECTER MAP | ΔMAP | rel% | p | win |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| Agricultural and Biological Sciences | 3,000 | 150 | 0.290 | 0.352 | 0.409 | 0.225 | 0.254 | +0.029 | +12.7 | 0.077 | ✓ |
| Arts and Humanities | 3,000 | 63 | 0.367 | 0.263 | 0.396 | 0.295 | 0.275 | -0.020 | -6.8 | 0.493 | ✗ |
| Biochemistry, Genetics and Molecular Biology | 3,000 | 258 | 0.439 | 0.418 | 0.266 | 0.211 | 0.211 | +0.000 | +0.2 | 0.980 | ✓ |
| Business, Management and Accounting | 3,000 | 215 | 0.294 | 0.368 | 0.294 | 0.141 | 0.163 | +0.023 | +16.2 | 0.097 | ✓ |
| Chemical Engineering | 3,000 | 683 | 0.305 | 0.445 | 0.218 | 0.098 | 0.085 | -0.013 | -13.4 | 0.015 | ✗ |
| Chemistry | 3,000 | 352 | 0.316 | 0.372 | 0.288 | 0.169 | 0.166 | -0.003 | -2.1 | 0.759 | ✗ |
| Computer Science | 3,000 | 528 | 0.484 | 0.652 | 0.202 | 0.135 | 0.195 | +0.060 | +44.1 | 0.000 | ✓ |
| Decision Sciences | 3,000 | 128 | 0.538 | 0.378 | 0.354 | 0.169 | 0.193 | +0.024 | +13.9 | 0.200 | ✓ |
| Earth and Planetary Sciences | 3,000 | 242 | 0.321 | 0.393 | 0.261 | 0.257 | 0.227 | -0.031 | -12.0 | 0.021 | ✗ |
| Economics, Econometrics and Finance | 3,000 | 101 | 0.331 | 0.427 | 0.230 | 0.196 | 0.205 | +0.010 | +4.9 | 0.680 | ✓ |
| Energy | 3,000 | 769 | 0.257 | 0.437 | 0.154 | 0.078 | 0.064 | -0.014 | -18.2 | 0.001 | ✗ |
| Engineering | 3,000 | 376 | 0.266 | 0.367 | 0.246 | 0.180 | 0.194 | +0.014 | +7.6 | 0.137 | ✓ |
| Environmental Science | 3,000 | 247 | 0.311 | 0.405 | 0.311 | 0.166 | 0.196 | +0.030 | +17.8 | 0.013 | ✓ |
| Immunology and Microbiology | 3,000 | 102 | 0.308 | 0.378 | 0.438 | 0.227 | 0.205 | -0.022 | -9.7 | 0.245 | ✗ |
| Materials Science | 3,000 | 367 | 0.253 | 0.372 | 0.305 | 0.168 | 0.188 | +0.020 | +11.9 | 0.049 | ✓ |
| Mathematics | 3,000 | 207 | 0.458 | 0.430 | 0.359 | 0.105 | 0.086 | -0.018 | -17.6 | 0.074 | ✗ |
| Medicine | 3,000 | 325 | 0.402 | 0.461 | 0.257 | 0.169 | 0.183 | +0.014 | +8.0 | 0.207 | ✓ |
| Neuroscience | 3,000 | 136 | 0.313 | 0.392 | 0.331 | 0.202 | 0.265 | +0.063 | +31.0 | 0.004 | ✓ |
| Nursing | 3,000 | 74 | 0.329 | 0.310 | 0.308 | 0.240 | 0.269 | +0.029 | +12.2 | 0.279 | ✓ |
| Pharmacology, Toxicology and Pharmaceutics | 3,000 | 292 | 0.235 | 0.340 | 0.266 | 0.146 | 0.112 | -0.034 | -23.5 | 0.001 | ✗ |
| Physics and Astronomy | 3,000 | 551 | 0.514 | 0.445 | 0.114 | 0.190 | 0.189 | -0.001 | -0.6 | 0.860 | ✗ |
| Psychology | 3,000 | 133 | 0.326 | 0.335 | 0.433 | 0.180 | 0.214 | +0.035 | +19.2 | 0.044 | ✓ |
| Social Sciences | 3,000 | 133 | 0.330 | 0.308 | 0.452 | 0.177 | 0.215 | +0.039 | +21.9 | 0.015 | ✓ |
| Veterinary | 3,000 | 134 | 0.314 | 0.352 | 0.192 | 0.293 | 0.298 | +0.005 | +1.6 | 0.825 | ✓ |
| Dentistry | 3,000 | 162 | 0.284 | 0.351 | 0.199 | 0.207 | 0.238 | +0.031 | +15.0 | 0.084 | ✓ |
| Health Professions | 3,000 | 60 | 0.378 | 0.282 | 0.468 | 0.226 | 0.208 | -0.018 | -8.1 | 0.507 | ✗ |

## Concentration (Gini by field)

- Citation-count Gini range across fields: **[0.235, 0.538]**
- PageRank Gini range across fields: **[0.263, 0.652]**

## Interdisciplinarity (fraction of references crossing field boundaries)

- Range across fields: **[0.114, 0.468]** (mean 0.298)

Measured within the union of all loaded fields: for each field, the fraction of its references (whose target is loaded in *some* field) that point to a target in a **different** top-level field.


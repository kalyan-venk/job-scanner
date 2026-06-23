#!/usr/bin/env python3
"""
Measures precision / recall / F1 of the title filter against hand-labeled REAL
job titles. WIDEST MODE: AI/ML roles AND general SWE roles are both wanted (1).
Only genuinely off-target roles (sales, marketing, recruiting, finance-domain,
manager/director/intern by policy) are 0.
"""
import scanner

cfg = scanner.load_cfg()

LABELED = [
    # --- AI/ML roles: wanted ---
    ("Machine Learning Engineer", 1),
    ("Senior ML Engineer, Recommendations", 1),
    ("Applied Scientist, Search", 1),
    ("AI Engineer, Evaluation", 1),
    ("Research Engineer, LLM", 1),
    ("Member of Technical Staff, Inference", 1),
    ("Data Scientist, Experimentation", 1),
    ("MLOps Engineer", 1),
    ("NLP Engineer", 1),
    ("Computer Vision Engineer", 1),
    ("Software Engineer, Model Serving", 1),
    ("Generative AI Engineer", 1),
    ("ML Infrastructure Engineer", 1),
    # --- General SWE roles: NOW wanted (widest mode) ---
    ("Software Engineer", 1),
    ("Software Engineer, Backend Payments", 1),
    ("Senior Software Engineer", 1),
    ("Software Development Engineer II", 1),
    ("Backend Engineer, Platform", 1),
    ("Full Stack Engineer", 1),
    ("Distributed Systems Engineer", 1),
    ("Platform Engineer, Compute", 1),
    ("Infrastructure Engineer", 1),
    ("Data Engineer, ETL Pipelines", 1),
    ("Site Reliability Engineer", 1),
    ("Founding Engineer", 1),
    ("Cloud Engineer, AWS", 1),
    ("Software Engineer, Frontend", 1),   # still wanted in widest mode
    # --- Genuinely off-target: not wanted ---
    ("Account Executive, Enterprise", 0),
    ("Financial Modeling Analyst", 0),
    ("Sales Engineer, AI Solutions", 0),
    ("Marketing Manager, Growth", 0),
    ("Technical Recruiter, Engineering", 0),
    ("Customer Success Manager", 0),
    ("Business Development Representative", 0),
    ("Solutions Consultant, AI", 0),
    ("Product Designer", 0),
    ("Senior Accountant", 0),
    ("Model Risk Analyst", 0),
    ("Program Manager, Data", 0),
    ("Engineering Manager, ML Platform", 0),  # policy: manager excluded
    ("Director, AI Strategy", 0),             # policy: director excluded
    ("ML Engineering Intern", 0),             # policy: intern excluded
    ("Product Manager, Pricing Models", 0),
]

tp = fp = tn = fn = 0
errors = []
for title, gold in LABELED:
    pred = 1 if scanner.title_matches(title, cfg) else 0
    if pred == 1 and gold == 1: tp += 1
    elif pred == 1 and gold == 0:
        fp += 1; errors.append(("FALSE POSITIVE", title))
    elif pred == 0 and gold == 0: tn += 1
    else:
        fn += 1; errors.append(("FALSE NEGATIVE", title))

precision = tp/(tp+fp) if (tp+fp) else 0.0
recall = tp/(tp+fn) if (tp+fn) else 0.0
f1 = 2*precision*recall/(precision+recall) if (precision+recall) else 0.0
acc = (tp+tn)/len(LABELED)

print(f"Labeled: {len(LABELED)} (pos={tp+fn}, neg={tn+fp})")
print(f"TP={tp} FP={fp} TN={tn} FN={fn}")
print(f"Accuracy : {acc:.3f}")
print(f"Precision: {precision:.3f}")
print(f"Recall   : {recall:.3f}")
print(f"F1       : {f1:.3f}")
if errors:
    print("\nMisclassifications:")
    for kind, t in errors:
        print(f"  {kind}: {t}")
else:
    print("\nZero misclassifications.")

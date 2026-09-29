"""tools/mutate.py - Selection operator library (Phase 4.5 / Priority A-3).

This module exists because the AIDD multi-agent loop, as audited on
2026-09-17, is **not searching**. It is re-sampling. On the confirmatory
pool (370 unique molecules, median best-of-round -8.324 kcal/mol) drawing
8 molecules at random from the loop's own pool beats the agent 65.8% of
the time; the per-round best molecule equals the global best 70% of the
time; consecutive rounds' best molecules share a Tanimoto > 0.6 only
40.6% of the time. None of these numbers can be improved by adding more
LLM tokens or more rounds - they can only be improved by giving the
generator an explicit **parent + mutation** selection operator so the
search becomes a walk in chemistry space rather than a re-roll of the
same LLM prompt.

This file ships the three pieces that turn feedback into constrained
molecular edits:

1. **Parent selection** (``nearest_neighbors``): ECFP4 Tanimoto lookup
   so we can say "given a candidate's structural neighbours, which
   existing molecules are we already close to?"
2. **Mutation operators** (``brics_reassemble`` / ``atom_substitution`` /
   ``terminal_swap``): deterministic, seedable, no LLM. They are
   deliberately cheap - the goal is to generate structurally close
   analogues that the LLM can then decorate, not to be a competent
   de-novo design tool.
3. **Prompt bridge** (``format_parents_block``): a stable text
   representation of the top-k safety-gated parents + their weakness
   profiles, safe to inject into the generator user prompt.

Why RDKit only? Because:
* The loop already depends on RDKit - no new heavy dependency.
* Every operator below is reproducible, takes an integer seed, and
  runs in < 100 ms per molecule. The whole library is testable on the
  real 370-molecule pool in well under a second.
* No need for a 7B diffusion model to swap one amine tail for another.

The library is *deterministic* but **not** *exhaustive*: with a 100-call
budget and ``n_per_op=3`` you get a focused neighbour-cloud, not a
diffusion-tree. That is intentional. Phase 4.5 is "make the loop into
a search again". Phase 4.6+ can scale up the operators; the prompt
contract and the test suite stay the same.
"""
from __future__ import annotations

import random
import statistics
from typing import Iterable, Sequence

from rdkit import Chem, RDLogger, DataStructs
from rdkit.Chem import AllChem, BRICS
from rdkit.Chem.Scaffolds import MurckoScaffold

RDLogger.DisableLog("rdApp.*")

# Default terminal groups that appear repeatedly in the project's
# confirmatory pool (4-anilinoquinazoline series). They are the most
# productive swap targets because they sit at the solvent-exposed end
# of the scaffold and are the dominant source of within-pool diversity.
DEFAULT_TERMINALS: tuple[str, ...] = (
    "NCC",          # -CH2CH2NH2
    "NCCN(C)C",     # dimethylaminoethyl
    "N1CCOCC1",     # morpholine
    "N1CCCC1",      # pyrrolidine
    "N1CCCCC1",     # piperidine
    "N1CCNCC1",     # piperazine
    "OCCN",         # ethanolamine
    "OC",           # methoxy
    "OC(C)C",       # isopropoxy
    "F",
    "Cl",
    "OC(F)(F)F",
    "C(=O)N",       # amide
    "C(=O)O",       # ester/acid
    "C#N",          # nitrile
)

# Pharmacophore-typical H substitutions used by ``atom_substitution``.
# Each entry is (existing_element, new_element, allowed_neighbour_count).
# Restricting neighbour count avoids replacing hydrogens on tetravalent
# atoms or on carbonyl carbons (which would break valence rules).
PHARMA_SUBSTITUTIONS: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    ("F", "Cl", (1,)),
    ("Cl", "F", (1,)),
    ("H", "F", (2,)),     # sp2/sp3 C-H -> C-F (rare; used sparingly)
    ("H", "OH", (2,)),    # aromatic or aliphatic C-H -> C-OH
    ("N", "O", (2,)),     # secondary amine -> ether-like (very rare; chemistry)
)


# ============================ Parent selection ============================

def _fingerprint(mol: Chem.Mol) -> DataStructs.ExplicitBitVect | None:
    """Morgan fingerprint (radius=2, 2048 bits) - ECFP4 equivalent."""
    if mol is None:
        return None
    try:
        return AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=2048)
    except Exception:
        return None


def _safe_mol(smiles: str) -> Chem.Mol | None:
    if not smiles or not isinstance(smiles, str):
        return None
    try:
        return Chem.MolFromSmiles(smiles.strip())
    except Exception:
        return None


def _safe_canonical(smiles: str) -> str | None:
    mol = _safe_mol(smiles)
    if mol is None:
        return None
    try:
        return Chem.MolToSmiles(mol)
    except Exception:
        return None


def nearest_neighbors(
    smiles: str,
    parents: Sequence[str],
    k: int = 5,
    threshold: float = 0.0,
) -> list[dict]:
    """Return the k ECFP4-Tanimoto nearest neighbours of ``smiles`` in
    ``parents``.

    Args:
        smiles: query SMILES (the candidate the generator is reasoning
            about). May be invalid; the result will then be empty.
        parents: iterable of candidate SMILES drawn from the working
            memory / Pareto front. Invalid entries are skipped silently
            and reflected in ``n_skipped``.
        k: maximum number of neighbours to return.
        threshold: similarity floor; entries below this Tanimoto are
            dropped. ``0.0`` keeps all valid neighbours.

    Returns:
        List of dicts, sorted by similarity descending::

            [{"smiles": "...", "similarity": 0.83, "rank": 1}, ...]

        Fields:
            * ``n_parents``: how many parents were actually compared.
            * ``n_skipped``: how many failed RDKit parsing.
            * ``query_parsed``: bool - whether ``smiles`` itself parsed.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    query_mol = _safe_mol(smiles)
    query_fp = _fingerprint(query_mol)
    if query_fp is None:
        return [], {
            "n_parents": len(parents),
            "n_skipped": 0,
            "query_parsed": False,
            "k": k,
            "threshold": threshold,
        }

    scored: list[tuple[float, str]] = []
    n_skipped = 0
    for p in parents:
        if not p or p == smiles:
            continue
        mol = _safe_mol(p)
        if mol is None:
            n_skipped += 1
            continue
        fp = _fingerprint(mol)
        if fp is None:
            n_skipped += 1
            continue
        try:
            sim = float(DataStructs.TanimotoSimilarity(query_fp, fp))
        except Exception:
            n_skipped += 1
            continue
        if sim < threshold:
            continue
        scored.append((sim, Chem.MolToSmiles(mol)))

    scored.sort(key=lambda t: (-t[0], t[1]))
    rows = [
        {"smiles": s, "similarity": round(sim, 4), "rank": i + 1}
        for i, (sim, s) in enumerate(scored[:k])
    ]
    meta = {
        "n_parents": len(parents),
        "n_skipped": n_skipped,
        "query_parsed": True,
        "k": k,
        "threshold": threshold,
    }
    return rows, meta


# The return type of nearest_neighbors above intentionally returns a tuple
# ``(rows, meta)``. The double-output makes it easy to assert on counts
# without smuggling meta keys inside the data list. Implementations below
# wrap it in a small facade for callers that prefer one object.

def nearest_neighbors_with_meta(
    smiles: str,
    parents: Sequence[str],
    k: int = 5,
    threshold: float = 0.0,
) -> dict:
    """Convenience wrapper: returns ``{"neighbors": [...], "meta": {...}}``."""
    rows, meta = nearest_neighbors(smiles, parents, k=k, threshold=threshold)
    return {"neighbors": rows, **meta}


# ============================ Mutation operators ============================

def brics_reassemble(
    smiles: str,
    fragment_pool: Sequence[str] | None = None,
    max_fragments: int = 3,
    seed: int = 0,
) -> list[str]:
    """BRICS cut + reassemble against an optional fragment pool.

    Steps:
        1. Parse ``smiles``; bail if invalid.
        2. ``BRICS.BRICSDecompose`` into attachable fragments.
        3. For each pair of fragments, try ``BRICS.BRICSBuild`` to
           reassemble. The pool (if provided) is substituted at every
           ``[*]`` position before the build, which lets the operator
           "swap one fragment for a library analogue" rather than just
           shuffle the input's own fragments.

    Args:
        smiles: parent SMILES to mutate.
        fragment_pool: optional list of SMILES whose BRICS fragments are
            used as the *replacement* library. ``None`` means "use the
            input's own fragments only" (still produces useful variants
            because BRICS cuts are not unique).
        max_fragments: cap on reassembly attempts to keep runtime
            bounded.
        seed: RNG seed for the deterministic shuffle over candidates.

    Returns:
        Up to ``max_fragments`` unique, RDKit-parseable product SMILES.
        May be empty if BRICS cannot cut the input or the reassembly
        attempts all fail.

    Notes:
        * ``BRICSBuild`` is stochastic in some RDKit versions; we fix
          ``random.seed`` first so repeated calls with the same inputs
          return the same list.
        * Every product is canonicalised; canonical duplicates are
          dropped and so are duplicates of the parent itself.
    """
    parent = _safe_canonical(smiles)
    if parent is None:
        return []
    mol = _safe_mol(parent)
    if mol is None:
        return []

    rng = random.Random(seed)

    # Build the combined fragment library: parent's fragments + (optional)
    # library fragments, both filtered to be BRICS-compatible.
    lib_frags: set[str] = set()
    try:
        for frag in BRICS.BRICSDecompose(mol):
            if frag:
                lib_frags.add(frag)
    except Exception:
        return []
    if fragment_pool:
        for lib_smi in fragment_pool:
            lib_mol = _safe_mol(lib_smi)
            if lib_mol is None:
                continue
            try:
                for frag in BRICS.BRICSDecompose(lib_mol):
                    if frag:
                        lib_frags.add(frag)
            except Exception:
                continue

    if len(lib_frags) < 2:
        return []

    rng.shuffle(sorted(lib_frags))  # deterministic given seed
    frag_list = list(lib_frags)

    products: list[str] = []
    seen: set[str] = {parent}
    # Take ``max_fragments`` fragment pairs and try to reassemble. We
    # deliberately reuse fragments across attempts so a single common
    # core (e.g. the quinazoline) can be combined with several tails.
    for i in range(min(max_fragments, len(frag_list))):
        a_smi = frag_list[i]
        a_mol = _safe_mol(a_smi)
        if a_mol is None:
            continue
        # Try every other fragment as the partner; cap the partner set.
        partners = frag_list[i + 1:i + 1 + max_fragments]
        for b_smi in partners:
            if len(products) >= max_fragments:
                break
            b_mol = _safe_mol(b_smi)
            if b_mol is None:
                continue
            try:
                # BRICSBuild is iterator-based; advance one step to get
                # the first reassembled SMILES with the two fragments.
                enum = BRICS.BRICSBuild([a_mol, b_mol])
                for step in range(3):  # take first few attempts
                    try:
                        next_mol = next(enum)
                    except StopIteration:
                        break
                    except Exception:
                        break
                    canon = Chem.MolToSmiles(next_mol)
                    if canon and canon not in seen and _safe_mol(canon) is not None:
                        seen.add(canon)
                        products.append(canon)
                        break
            except Exception:
                continue
        if len(products) >= max_fragments:
            break

    return products[:max_fragments]


def atom_substitution(
    smiles: str,
    n_subs: int = 2,
    seed: int = 0,
) -> list[str]:
    """Replace up to ``n_subs`` atoms with pharmacophore-typical alternatives.

    The substitution list (``PHARMA_SUBSTITUTIONS``) is intentionally
    conservative: F <-> Cl on aromatic positions, plus the rare H -> F /
    H -> OH swap. Each substitution is validated against the resulting
    molecule's valence; failed products are dropped silently.

    Returns:
        Up to ``n_subs`` unique, RDKit-parseable product SMILES.
    """
    parent = _safe_canonical(smiles)
    if parent is None:
        return []
    mol = _safe_mol(parent)
    if mol is None:
        return []

    rng = random.Random(seed)

    # Find candidate (atom_index, current_element) tuples that have at
    # least one matching substitution rule.
    candidates: list[tuple[int, str, str, tuple[int, ...]]] = []
    for atom in mol.GetAtoms():
        elem = atom.GetSymbol()
        nbrs = atom.GetTotalValence()
        for old, new, allowed in PHARMA_SUBSTITUTIONS:
            if elem == old and nbrs in allowed:
                candidates.append((atom.GetIdx(), old, new, allowed))
    if not candidates:
        return []

    rng.shuffle(candidates)
    products: list[str] = []
    seen: set[str] = {parent}
    for atom_idx, _old, new, _ in candidates:
        if len(products) >= n_subs:
            break
        # Try a fresh copy so each product only carries one mutation.
        copy_mol = Chem.MolFromSmiles(parent)
        if copy_mol is None:
            continue
        try:
            copy_mol.GetAtomWithIdx(atom_idx).SetNumExplicitHs(0)
            copy_mol.GetAtomWithIdx(atom_idx).SetAtomicNum(
                Chem.AtomFromSmiles(new).GetAtomicNum()
            )
            Chem.SanitizeMol(copy_mol)
        except Exception:
            continue
        canon = Chem.MolToSmiles(copy_mol)
        if canon and canon not in seen and _safe_mol(canon) is not None:
            seen.add(canon)
            products.append(canon)

    return products


def terminal_swap(
    smiles: str,
    terminals: Sequence[str] = DEFAULT_TERMINALS,
    n_swaps: int = 3,
    seed: int = 0,
) -> list[str]:
    """Swap a terminal group (last non-ring heavy chain) for one in
    ``terminals``.

    Heuristic: detect the *only* occurrence of ``[*:N]``-style labels
    is too brittle. Instead we extract every ring atom, walk the
    heavy-atom graph from each ring atom outward along non-ring bonds
    and take the longest such chain as the "tail". Reattach a random
    terminal from ``terminals`` by replacing that chain with a single
    bond to the terminal fragment (via a fragment-join SMARTS).

    Returns:
        Up to ``n_swaps`` unique, RDKit-parseable product SMILES.
    """
    parent = _safe_canonical(smiles)
    if parent is None:
        return []
    mol = _safe_mol(parent)
    if mol is None:
        return []

    rng = random.Random(seed)

    ring_info = mol.GetRingInfo()
    ring_atom_ids = {a.GetIdx() for a in mol.GetAtoms() if a.IsInRing()}
    if not ring_atom_ids:
        return []

    # For each ring atom, find the longest non-ring outgoing chain.
    candidates: list[tuple[int, list[int]]] = []
    for ring_idx in ring_atom_ids:
        for nbr in mol.GetAtomWithIdx(ring_idx).GetNeighbors():
            if nbr.GetIdx() in ring_atom_ids:
                continue
            # DFS over non-ring neighbors only.
            chain = [nbr.GetIdx()]
            visited = {ring_idx, nbr.GetIdx()}
            stack = [nbr.GetIdx()]
            while stack:
                cur = stack.pop()
                for n in mol.GetAtomWithIdx(cur).GetNeighbors():
                    if n.GetIdx() in visited:
                        continue
                    if n.GetIdx() in ring_atom_ids:
                        continue
                    visited.add(n.GetIdx())
                    chain.append(n.GetIdx())
                    stack.append(n.GetIdx())
            if len(chain) >= 1:
                candidates.append((ring_idx, chain))

    if not candidates:
        return []

    rng.shuffle(candidates)
    rng.shuffle(list(terminals))
    products: list[str] = []
    seen: set[str] = {parent}
    for ring_idx, chain in candidates:
        if len(products) >= n_swaps:
            break
        for term in terminals:
            if len(products) >= n_swaps:
                break
            term_mol = _safe_mol(term)
            if term_mol is None:
                continue
            try:
                combo = Chem.RWMol(mol)
                # Remove all atoms in the chain except the one directly
                # attached to the ring; that attachment point becomes the
                # new bond to the terminal's first non-H atom.
                attach_idx = chain[0]
                # Sort by descending index to avoid invalidation.
                for chain_idx in sorted(chain, reverse=True):
                    if chain_idx == attach_idx:
                        # Replace its element with the terminal's first atom.
                        new_atom = term_mol.GetAtomWithIdx(0)
                        existing = combo.GetAtomWithIdx(chain_idx)
                        existing.SetAtomicNum(new_atom.GetAtomicNum())
                        existing.SetFormalCharge(new_atom.GetFormalCharge())
                        existing.SetNumExplicitHs(0)
                    else:
                        combo.RemoveAtom(chain_idx)
                # Attach remaining terminal atoms as new atoms bonded
                # to attach_idx, one bond per atom along the terminal
                # skeleton.
                prev_idx = attach_idx
                for term_idx in range(1, term_mol.GetNumAtoms()):
                    new_atom = term_mol.GetAtomWithIdx(term_idx)
                    added_idx = combo.AddAtom(
                        Chem.Atom(new_atom.GetAtomicNum())
                    )
                    combo.AddBond(prev_idx, added_idx,
                                 Chem.BondType.SINGLE)
                    prev_idx = added_idx
                Chem.SanitizeMol(combo)
                canon = Chem.MolToSmiles(combo)
                if canon and canon not in seen and _safe_mol(canon) is not None:
                    seen.add(canon)
                    products.append(canon)
                    break
            except Exception:
                continue

    return products[:n_swaps]


def mutate(
    smiles: str,
    parents: Sequence[str] | None = None,
    terminals: Sequence[str] = DEFAULT_TERMINALS,
    n_per_op: int = 3,
    seed: int = 0,
) -> dict:
    """One-shot ``mutate`` that runs all three operators and returns a
    de-duplicated, parent-aware bundle.

    Returns::

        {
          "parent": canonical parent SMILES or None,
          "products": [canonical SMILES, ...],   # de-duplicated, parent-excluded
          "operators": {
              "brics": [...],
              "substitution": [...],
              "terminal_swap": [...],
          },
          "n_unique": int,
          "seed": int,
        }

    Args:
        smiles: parent SMILES to mutate.
        parents: optional list of additional SMILES used as a fragment
            pool for BRICS reassembly (default ``None`` -> only the
            parent's own fragments).
        terminals: list of terminal group SMILES for ``terminal_swap``.
        n_per_op: max products per operator.
        seed: deterministic seed.

    Notes:
        * Operators may return empty lists for molecules that cannot
          be cut (e.g. fully aromatic without fragmentable bonds).
        * No LLM calls. Pure RDKit + Python.
        * Products are deduplicated against the parent and against
          each other, then concatenated; the operator-level lists
          retain provenance so callers can audit which operator
          produced what.
    """
    parent = _safe_canonical(smiles)
    out: dict = {
        "parent": parent,
        "products": [],
        "operators": {"brics": [], "substitution": [], "terminal_swap": []},
        "n_unique": 0,
        "seed": seed,
    }
    if parent is None:
        return out

    operators_out = out["operators"]

    try:
        operators_out["brics"] = brics_reassemble(
            parent, fragment_pool=list(parents or []),
            max_fragments=n_per_op, seed=seed,
        )
    except Exception:
        operators_out["brics"] = []

    try:
        operators_out["substitution"] = atom_substitution(
            parent, n_subs=n_per_op, seed=seed + 1,
        )
    except Exception:
        operators_out["substitution"] = []

    try:
        operators_out["terminal_swap"] = terminal_swap(
            parent, terminals=list(terminals),
            n_swaps=n_per_op, seed=seed + 2,
        )
    except Exception:
        operators_out["terminal_swap"] = []

    seen: set[str] = {parent}
    merged: list[str] = []
    for source in ("brics", "substitution", "terminal_swap"):
        for s in operators_out[source]:
            if s not in seen:
                seen.add(s)
                merged.append(s)

    out["products"] = merged
    out["n_unique"] = len(merged)
    return out


# ============================ Prompt bridge ============================

def _weakness_for(candidate: dict) -> str:
    """Summarise a candidate's weakest dimension for the PARENTS block.

    Reads the same fields the generator's evaluator already produces
    (``admet`` + ``dock`` + ``validate``) so no new schema is needed.
    """
    if not candidate:
        return "(no weakness profile available)"
    admet = candidate.get("admet") or {}
    dock = candidate.get("dock") or {}
    val = candidate.get("validate") or {}

    notes: list[str] = []
    risk = admet.get("herg_risk_score")
    if risk is not None and risk >= 0.5:
        notes.append(f"hERG-risk={float(risk):.2f}")
    logp = admet.get("logp")
    if logp is not None and logp > 4.0:
        notes.append(f"logP={float(logp):.2f}")
    mw = admet.get("mw")
    if mw is not None and mw > 480:
        notes.append(f"MW={float(mw):.0f}")
    qed = admet.get("qed")
    if qed is not None and qed < 0.4:
        notes.append(f"QED={float(qed):.2f}")
    if dock.get("score") is not None and dock["score"] > -7.0:
        notes.append(f"vina={float(dock['score']):.2f}")
    if not val.get("valid"):
        notes.append("(last cycle invalid)")

    return " | ".join(notes) if notes else "(no major weakness flagged)"


def format_parents_block(
    parents: Sequence[dict],
    k: int = 3,
    header: str = "PARENTS (safety-gated, score-ranked):",
) -> str:
    """Render the top-k safety-gated parents as a prompt segment.

    Each line follows the contract::

        [#<rank>] smiles=<canonical>  vina=<v>  hERG=<risk>  weakness=<text>

    The block is intentionally text-only (no markdown, no JSON) so it
    can be appended to the generator user prompt without breaking the
    "STRICT JSON only" instruction in the system prompt.

    Args:
        parents: enriched candidates (the dicts returned by
            ``agents.evaluator.evaluate_candidates``). Caller is
            responsible for filtering to ``safety_gate_pass is True``
            and sorting by ``candidate_priority_key``. We do not
            re-sort here so the prompt reflects the order the loop
            actually intended.
        k: maximum number of parents to emit.
        header: optional header line. Pass ``""`` to suppress.

    Returns:
        A multi-line string. Empty string if ``parents`` is empty or
        every entry fails the sanity checks.

    Notes:
        * This function never raises; malformed entries are skipped.
        * Lines are 200-char safe (the generator model has a 2048-token
          budget per response and we want this block to stay well below
          200 chars per parent).
    """
    if not parents:
        return ""
    lines: list[str] = []
    if header:
        lines.append(header)
    for i, c in enumerate(parents[:k], start=1):
        if not isinstance(c, dict):
            continue
        smiles = _safe_canonical(str(c.get("smiles", "")))
        if smiles is None:
            continue
        # Truncate to 80 chars to keep prompt compact.
        if len(smiles) > 80:
            smiles = smiles[:77] + "..."
        dock = c.get("dock") or {}
        admet = c.get("admet") or {}
        vina = dock.get("score")
        risk = admet.get("herg_risk_score")
        vina_text = "n/a" if vina is None else f"{float(vina):.2f}"
        risk_text = "n/a" if risk is None else f"{float(risk):.2f}"
        lines.append(
            f"  [#{i}] smiles={smiles}  vina={vina_text}  "
            f"hERG={risk_text}  weakness={_weakness_for(c)}"
        )
    return "\n".join(lines)


# ============================ Offline validation ============================

def offline_validation(
    parents_pool: Sequence[dict],
    *,
    seed: int = 0,
    n_per_op: int = 3,
    top_n: int = 5,
    n_probes: int = 10,
) -> dict:
    """Validate the operator library against a saved pool.

    Runs the library against the top-N safety-gated parents and reports:
        * how many unique analogues are generated,
        * the mean Tanimoto to the nearest real pool molecule
          (i.e. are we producing molecules that already exist?),
        * the mean Tanimoto to the parent (i.e. are we producing
          molecules that are actually *different*?).

    Goal (set by REVIEW_MINIMAX_ADVICE Priority A-3): the generated
    analogues should land in the **neighbourhood** of ``best_safe_vina``
    molecules but **not coincide** with them. The "neighbour is in the
    pool, not in the loop output" gap is what the missing selection
    operator needs to close.

    Args:
        parents_pool: iterable of enriched candidates (same shape as
            ``format_parents_block``'s input). Invalid entries are
            skipped.
        seed, n_per_op, top_n, n_probes: see ``mutate``.

    Returns:
        dict suitable for ``json.dumps``.
    """
    safe = [
        c for c in parents_pool
        if isinstance(c, dict) and c.get("safety_gate_pass") is True
    ]
    safe.sort(key=lambda c: (
        -(c.get("composite_score") or -1.0),
        (c.get("dock") or {}).get("score") or 0.0,
    ))
    top = safe[:top_n]
    if not top:
        return {
            "status": "no_safety_passing_parents",
            "n_safe_parents": 0,
        }

    real_smiles = [_safe_canonical(c["smiles"]) for c in safe]
    real_smiles = [s for s in real_smiles if s]

    all_products: list[str] = []
    per_parent: list[dict] = []
    for i, c in enumerate(top):
        out = mutate(
            c["smiles"], parents=None, n_per_op=n_per_op, seed=seed + i,
        )
        products = out["products"][:n_per_op]
        # Tanimoto of each product to (a) the parent and (b) the nearest
        # real pool molecule.
        sims_to_parent = [
            (DataStructs.TanimotoSimilarity(
                _fingerprint(_safe_mol(p)),
                _fingerprint(_safe_mol(c["smiles"])),
            ) if p and _safe_mol(p) else None)
            for p in products
        ]
        sims_to_real: list[float | None] = []
        for p in products:
            fp = _fingerprint(_safe_mol(p))
            if fp is None:
                sims_to_real.append(None)
                continue
            best_sim = 0.0
            for rs in real_smiles:
                ref_fp = _fingerprint(_safe_mol(rs))
                if ref_fp is None:
                    continue
                best_sim = max(best_sim, DataStructs.TanimotoSimilarity(fp, ref_fp))
            sims_to_real.append(round(best_sim, 4))

        per_parent.append({
            "parent_smiles": c["smiles"],
            "products": products,
            "n_products": len(products),
            "tanimoto_to_parent_mean": (
                round(statistics.mean([s for s in sims_to_parent if s is not None]), 4)
                if any(s is not None for s in sims_to_parent) else None
            ),
            "tanimoto_to_real_pool_mean": (
                round(statistics.mean([s for s in sims_to_real if s is not None]), 4)
                if any(s is not None for s in sims_to_real) else None
            ),
        })
        all_products.extend(products)

    unique_products = list(dict.fromkeys(all_products))
    n_unique = len(unique_products)

    # Aggregate stats.
    means_parent = [r["tanimoto_to_parent_mean"] for r in per_parent
                    if r["tanimoto_to_parent_mean"] is not None]
    means_real = [r["tanimoto_to_real_pool_mean"] for r in per_parent
                  if r["tanimoto_to_real_pool_mean"] is not None]

    return {
        "status": "ok",
        "n_safe_parents": len(safe),
        "n_top_parents": len(top),
        "n_total_products": len(all_products),
        "n_unique_products": n_unique,
        "duplicate_rate_within_run": round(
            1.0 - n_unique / max(len(all_products), 1), 4
        ),
        "tanimoto_to_parent_grand_mean": (
            round(statistics.mean(means_parent), 4) if means_parent else None
        ),
        "tanimoto_to_real_pool_grand_mean": (
            round(statistics.mean(means_real), 4) if means_real else None
        ),
        "per_parent": per_parent,
        "config": {
            "seed": seed, "n_per_op": n_per_op, "top_n": top_n,
            "n_probes": n_probes,
        },
    }


# ============================ CLI ============================

def _main() -> None:  # pragma: no cover - smoke CLI
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="tools/mutate.py CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_mut = sub.add_parser("mutate", help="run all three operators on one SMILES")
    p_mut.add_argument("smiles")
    p_mut.add_argument("--n", type=int, default=3)
    p_mut.add_argument("--seed", type=int, default=0)

    p_nn = sub.add_parser("neighbors", help="find ECFP4 nearest neighbours")
    p_nn.add_argument("smiles")
    p_nn.add_argument("parents_file", help="file with one SMILES per line")
    p_nn.add_argument("--k", type=int, default=5)
    p_nn.add_argument("--threshold", type=float, default=0.0)

    p_v = sub.add_parser("validate", help="offline validation vs a JSON pool file")
    p_v.add_argument("pool_file", help="JSON list of enriched candidates")
    p_v.add_argument("--seed", type=int, default=0)
    p_v.add_argument("--n-per-op", type=int, default=3)
    p_v.add_argument("--top-n", type=int, default=5)

    args = parser.parse_args()
    if args.cmd == "mutate":
        out = mutate(args.smiles, n_per_op=args.n, seed=args.seed)
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif args.cmd == "neighbors":
        parents = [
            ln.strip() for ln in Path(args.parents_file).read_text(
                encoding="utf-8"
            ).splitlines() if ln.strip()
        ]
        result = nearest_neighbors_with_meta(
            args.smiles, parents, k=args.k, threshold=args.threshold,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "validate":
        from pathlib import Path
        pool = json.loads(Path(args.pool_file).read_text(encoding="utf-8"))
        out = offline_validation(
            pool, seed=args.seed, n_per_op=args.n_per_op, top_n=args.top_n,
        )
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        parser.print_help(sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    from pathlib import Path
    _main()
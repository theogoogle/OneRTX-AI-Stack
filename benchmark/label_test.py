"""Check the router's LFM3 labeller on everyday questions, coding requests and hard reasoning.
python label_test.py            -> accuracy per category and every miss"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"%USERPROFILE%\.lmstudio\scripts")
import router

ITEMS = [
    # everyday questions -> fast (Gemma)
    ("fast", "how can one open a suitcase if broken"),
    ("fast", "What's a good substitute for buttermilk in pancakes?"),
    ("fast", "Is it safe to take ibuprofen and paracetamol together?"),
    ("fast", "What should I see in Lisbon in two days with kids?"),
    ("fast", "Why is the sky blue?"),
    ("fast", "How much is a 15% tip on 84 euros?"),
    ("fast", "Write a polite email to my landlord asking to fix the boiler this week."),
    ("fast", "Translate 'see you next Tuesday at the station' into Spanish."),
    ("fast", "What's the difference between a Roth IRA and a traditional IRA?"),
    ("fast", "Give me 5 ideas for a 7-year-old's birthday party at home."),
    ("fast", "Who won the 1998 football World Cup?"),
    ("fast", "How long should I boil an egg for a runny yolk?"),
    # coding -> code
    ("code", "Why does my React useEffect run twice in development?"),
    ("code", "Write a Python function that removes duplicates from a list but keeps the order."),
    ("code", "My Docker build fails with 'COPY failed: file not found in build context'. How do I fix it?"),
    ("code", "Convert this bash loop to PowerShell: for f in *.txt; do wc -l $f; done"),
    ("code", "Write a SQL query for the top 3 customers by revenue per country."),
    ("code", "Explain what this regex does: ^(?=.*\\d)(?=.*[a-z]).{8,}$"),
    ("code", "Add a --verbose flag to the CLI in this project and update the tests."),
    ("code", "The tests in tests/test_api.py fail with KeyError: 'user_id'. Fix it."),
    ("code", "Refactor this function to be async and add type hints."),
    ("code", "How do I undo my last git commit but keep the changes?"),
    ("code", "Set up a GitHub Actions workflow that runs pytest on every push."),
    ("code", "What does this stack trace mean: TypeError: Cannot read properties of undefined (reading 'map')"),
    # hard reasoning -> deep
    ("deep", "Design a sharding strategy for a 2-billion-row orders table and compare range vs hash sharding."),
    ("deep", "Prove that there are infinitely many prime numbers."),
    ("deep", "Plan a 6-month migration of a monolith to event-driven microservices, with risks and milestones."),
    ("deep", "Compare three architectures for a real-time collaborative editor (OT, CRDT, server-authoritative) and recommend one for a 5-person team."),
    ("deep", "In how many ways can 8 rooks be placed on a chessboard so that none attack each other and none is on the main diagonal? Explain the reasoning."),
    ("deep", "Five people each pick one day of a 10-day festival at random. What is the probability that at least two pick the same day?"),
    ("deep", "Our startup has 9 months of runway. Analyse the trade-offs between raising now, cutting burn by 30%, or pivoting to enterprise."),
    ("deep", "Derive the time complexity of building a binary heap bottom-up and explain why it is O(n)."),
    ("deep", "Design the data model and consistency strategy for a multi-region payments ledger."),
    ("deep", "A logic puzzle: four houses, four owners, four pets, nine clues... work out who owns the fish step by step."),
    ("deep", "Think through the second-order effects of a 4-day work week on a 200-person software company."),
    ("deep", "Plan the full curriculum for teaching myself machine learning in 12 months, with milestones and projects."),
]


def main():
    per, misses = {}, []
    for want, text in ITEMS:
        got = router.classify_with_lfm(text)
        ok, n = per.get(want, (0, 0))
        per[want] = (ok + (got == want), n + 1)
        if got != want:
            misses.append(f"  want {want:4} got {got:4}  {text[:90]}")
    total = sum(o for o, _ in per.values())
    print(f"{total}/{len(ITEMS)} right | " + " | ".join(f"{k}: {o}/{n}" for k, (o, n) in per.items()))
    print("\n".join(misses))


if __name__ == "__main__":
    main()

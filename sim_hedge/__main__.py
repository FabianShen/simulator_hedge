"""Run the Lesson 1 example with ``python -m sim_hedge``."""

from sim_hedge.market import generate_market_ticks


def main() -> None:
    changes = (0.02, -0.01, 0.03, -0.02)

    print("step  spot")
    for tick in generate_market_ticks(initial_spot=2.00, price_changes=changes):
        print(f"{tick.step:>4}  {tick.spot:.2f}")


if __name__ == "__main__":
    main()


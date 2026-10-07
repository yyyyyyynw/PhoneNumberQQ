import csv


def generate_combinations():
    for i in range(1_000_000):
        yield f"134{i:06d}59"


if __name__ == "__main__":
    filename = "combinations.csv"

    with open(filename, "w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerows([[number] for number in generate_combinations()])

    print(f"完成：1,000,000 種組合已儲存至 {filename}")
import os
from datetime import datetime

def main():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    input_file = os.path.join(root_dir, "linkgrabber.txt")

    if not os.path.exists(input_file):
        print("Error: linkgrabber.txt not found in this directory.")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_file = os.path.join(root_dir, f"{timestamp}.txt")

    with open(input_file, "r", encoding="utf-8") as infile, open(output_file, "w", encoding="utf-8") as outfile:
        for i, line in enumerate(infile, start=1):
            if i % 13 in (1, 2, 3):
                outfile.write(line)

    print(f"Created {output_file} with the first 3 lines of every 13-line group from linkgrabber.txt")

if __name__ == "__main__":
    main()

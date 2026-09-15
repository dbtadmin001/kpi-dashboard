"""Download shared Flink extensions once; verify the published Maven checksum."""
import hashlib
from pathlib import Path
import requests

ARTIFACTS = [
    ("org/apache/iceberg", "iceberg-flink-runtime-1.20", "1.9.2"),
    ("org/apache/iceberg", "iceberg-aws-bundle", "1.9.2"),
    ("org/apache/flink", "flink-sql-connector-kafka", "3.3.0-1.20"),
    # Iceberg's Flink runtime resolves org.apache.hadoop.conf.Configuration even
    # for a REST catalog on S3FileIO; this shaded uber jar supplies it alone.
    ("org/apache/flink", "flink-shaded-hadoop-2-uber", "2.8.3-10.0"),
]


def main():
    dest = Path(__file__).parent / "jars"
    dest.mkdir(exist_ok=True)
    for group, artifact, version in ARTIFACTS:
        name = f"{artifact}-{version}.jar"
        url = f"https://repo.maven.apache.org/maven2/{group}/{artifact}/{version}/{name}"
        algorithm = "sha512"
        check = requests.get(url + ".sha512", timeout=30)
        if check.status_code == 404:
            algorithm = "sha1"  # Older Flink artifacts publish only SHA-1.
            check = requests.get(url + ".sha1", timeout=30)
        check.raise_for_status()
        expected = check.text.strip().split()[0]
        target = dest / name
        if target.exists() and hashlib.new(algorithm, target.read_bytes()).hexdigest() == expected:
            print(f"Verified cached {name}")
            continue
        response = requests.get(url, timeout=180)
        response.raise_for_status()
        if hashlib.new(algorithm, response.content).hexdigest() != expected:
            raise ValueError(f"Checksum mismatch: {name}")
        target.write_bytes(response.content)
        print(f"Downloaded and verified {name}")


if __name__ == "__main__":
    main()

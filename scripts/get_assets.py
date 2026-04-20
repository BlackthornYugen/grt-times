import hashlib
import httpx
from pathlib import Path

# Version and expected SHA-256 checksums for Swagger UI assets
VERSION = "5.17.14"
BASE_URL = f"https://unpkg.com/swagger-ui-dist@{VERSION}"
EXPECTED_HASHES = {
    "swagger-ui-bundle.js": "c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1",
    "swagger-ui.css": "40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117",
    "favicon-32x32.png": "3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837",
}

def get_file_hash(path: Path) -> str:
    """Calculate SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def main():
    swagger_dir = Path("static/swagger-ui")
    swagger_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Ensuring Swagger UI {VERSION} assets are present and verified...")
    
    with httpx.Client(follow_redirects=True) as client:
        for filename, expected_hash in EXPECTED_HASHES.items():
            file_path = swagger_dir / filename
            
            if file_path.exists():
                actual_hash = get_file_hash(file_path)
                if actual_hash == expected_hash:
                    print(f"  - {filename}: matches checksum, skipping.")
                    continue
                else:
                    print(f"  - {filename}: checksum mismatch, re-downloading...")
            
            print(f"  - {filename}: downloading...")
            resp = client.get(f"{BASE_URL}/{filename}")
            resp.raise_for_status()
            
            # Verify downloaded content before saving
            downloaded_hash = hashlib.sha256(resp.content).hexdigest()
            if downloaded_hash != expected_hash:
                print(f"CRITICAL ERROR: {filename} from CDN failed checksum verification!")
                print(f"Expected: {expected_hash}")
                print(f"Got:      {downloaded_hash}")
                exit(1)
                
            file_path.write_bytes(resp.content)
            print(f"  - {filename}: downloaded and verified.")

if __name__ == "__main__":
    main()

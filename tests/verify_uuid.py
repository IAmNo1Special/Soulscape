import os

from soulscape.core.soul import Soul

DB_FILE = "soulscape_hub.db"
LOCAL_SOULS = "souls.json"


def cleanup():
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    if os.path.exists(LOCAL_SOULS):
        os.remove(LOCAL_SOULS)


def verify_uuid_generation():
    print("Verifying UUID generation...")
    s1 = Soul(
        orb_color_rgb=(1, 1, 1), aura_color_rgb=(1, 1, 1), name="Test Soul 1"
    )
    s2 = Soul(
        orb_color_rgb=(1, 1, 1), aura_color_rgb=(1, 1, 1), name="Test Soul 2"
    )

    print(f"Soul 1 ID: {s1.biology.soul_id}")
    print(f"Soul 2 ID: {s2.biology.soul_id}")

    if s1.biology.soul_id == s2.biology.soul_id:
        print("FAIL: IDs are identical!")
        return False

    if isinstance(s1.biology.soul_id, int):
        print("FAIL: ID is an integer!")
        return False

    if len(str(s1.biology.soul_id)) < 10:  # simplistic check for UUID length
        print("FAIL: ID seems too short for a UUID!")
        return False

    print("PASS: IDs are unique and look like UUIDs.")
    return True


def verify_persistence():
    print("Verifying persistence...")
    s1 = Soul(
        orb_color_rgb=(1, 1, 1), aura_color_rgb=(1, 1, 1), name="Persist Soul"
    )
    original_id = s1.biology.soul_id

    data = s1.to_dict()
    s2 = Soul.from_dict(data)

    print(f"Original ID: {original_id}")
    print(f"Restored ID: {s2.biology.soul_id}")

    if original_id != s2.biology.soul_id:
        print("FAIL: ID changed after serialization!")
        return False

    print("PASS: ID persisted correctly.")
    return True


if __name__ == "__main__":
    try:
        if verify_uuid_generation() and verify_persistence():
            print("\nALL CHECKS PASSED")
        else:
            print("\nCHECKS FAILED")
    except Exception as e:
        print(f"An error occurred: {e}")

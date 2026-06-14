from strategy_lab.encrypted_bundle import decrypt_file, encrypt_file


def test_encrypt_and_decrypt_file_round_trips_payload(tmp_path) -> None:
    source = tmp_path / "source.zip"
    encrypted = tmp_path / "source.zip.encrypted"
    decrypted = tmp_path / "decrypted.zip"
    source.write_bytes(b"private research payload")

    encrypt_file(input_path=source, output_path=encrypted, passphrase="test-passphrase")
    decrypt_file(input_path=encrypted, output_path=decrypted, passphrase="test-passphrase")

    assert encrypted.read_bytes() != source.read_bytes()
    assert decrypted.read_bytes() == source.read_bytes()


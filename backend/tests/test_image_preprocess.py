import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image
from PIL.ExifTags import IFD
from PIL.TiffImagePlugin import IFDRational
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, ImageRow, create_sqlite_engine
from app.models.enums import ImageStatus
from app.persistence.analyses import (
    add_uploaded_image,
    create_book,
    create_draft,
    record_preprocessed_image,
)
from app.services.images.preprocess import VISION_LONGEST_EDGE, preprocess_image


def _with_gps(width: int, height: int, image_format: str) -> bytes:
    image = Image.new("RGB", (width, height), (12, 80, 160))
    exif = image.getexif()
    exif[271] = "LoanLensTest"
    gps = exif.get_ifd(IFD.GPSInfo)
    gps[1] = "N"
    gps[2] = (IFDRational(12, 1), IFDRational(34, 1), IFDRational(56, 1))
    gps[3] = "E"
    gps[4] = (IFDRational(77, 1), IFDRational(35, 1), IFDRational(0, 1))
    buffer = BytesIO()
    image.save(buffer, format=image_format, exif=exif)
    return buffer.getvalue()


def _assert_input_has_gps(data: bytes) -> None:
    with Image.open(BytesIO(data)) as image:
        gps = image.getexif().get_ifd(IFD.GPSInfo)
        assert gps[1] == "N"
        assert "exif" in image.info


def _assert_no_exif(path: Path) -> None:
    with Image.open(path) as image:
        assert "exif" not in image.info
        assert len(image.getexif()) == 0
        assert not image.getexif().get_ifd(IFD.GPSInfo)


def test_jpeg_gps_is_stripped_and_small_image_is_not_upscaled(tmp_path: Path) -> None:
    raw = _with_gps(400, 300, "JPEG")
    _assert_input_has_gps(raw)

    prepared = preprocess_image(
        raw,
        tmp_dir=tmp_path,
        analysis_id="anl_preprocess",
        image_id="img_jpeg",
        content_type="image/jpeg",
    )

    stored = Path(prepared.storage_path)
    assert stored == (tmp_path / "anl_preprocess" / "img_jpeg").resolve()
    assert stored.is_file()
    assert prepared.content_type == "image/jpeg"
    assert prepared.width == 400
    assert prepared.height == 300
    assert prepared.byte_size == stored.stat().st_size
    assert prepared.checksum_sha256 == hashlib.sha256(stored.read_bytes()).hexdigest()
    _assert_no_exif(stored)


def test_png_has_no_exif_key(tmp_path: Path) -> None:
    raw = _with_gps(220, 240, "PNG")
    _assert_input_has_gps(raw)

    prepared = preprocess_image(
        raw,
        tmp_dir=tmp_path,
        analysis_id="anl_preprocess",
        image_id="img_png",
        content_type="image/png",
    )

    stored = Path(prepared.storage_path)
    assert stored == (tmp_path / "anl_preprocess" / "img_png").resolve()
    assert prepared.content_type == "image/png"
    assert prepared.width == 220
    assert prepared.height == 240
    _assert_no_exif(stored)


def test_longest_edge_downscales_to_2048(tmp_path: Path) -> None:
    raw = _with_gps(3000, 1500, "JPEG")

    prepared = preprocess_image(
        raw,
        tmp_dir=tmp_path,
        analysis_id="anl_preprocess",
        image_id="img_large",
    )

    assert prepared.width == VISION_LONGEST_EDGE
    assert prepared.height == 1024
    assert max(prepared.width, prepared.height) == 2048
    with Image.open(prepared.storage_path) as image:
        assert image.size == (2048, 1024)
        assert "exif" not in image.info


def test_row_stores_checksum_and_path_without_image_bytes(tmp_path: Path) -> None:
    raw = _with_gps(400, 320, "JPEG")
    engine = create_sqlite_engine(f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        book = create_book(session)
        draft = create_draft(session, book.id, 24)
        prepared = preprocess_image(
            raw,
            tmp_dir=tmp_path / "images",
            analysis_id=draft.id,
            image_id="img_db",
            content_type="image/jpeg",
        )
        file_bytes = Path(prepared.storage_path).read_bytes()
        assert Path(prepared.storage_path) == (tmp_path / "images" / draft.id / "img_db").resolve()
        row = add_uploaded_image(
            session,
            image_id="img_db",
            analysis_id=draft.id,
            filename="statement.jpg",
            content_type="image/jpeg",
            byte_size=len(raw),
            storage_path="pending",
        )
        record_preprocessed_image(
            session,
            row,
            checksum_sha256=prepared.checksum_sha256,
            width=prepared.width,
            height=prepared.height,
            storage_path=prepared.storage_path,
            content_type=prepared.content_type,
            byte_size=prepared.byte_size,
        )
        session.commit()

        for column in inspect(engine).get_columns("images"):
            assert "BLOB" not in str(column["type"]).upper()

        stored = session.get(ImageRow, "img_db")
        assert stored is not None
        assert stored.status == ImageStatus.PREPROCESSED.value
        assert stored.checksum_sha256 == hashlib.sha256(file_bytes).hexdigest()
        assert stored.checksum_sha256 is not None
        assert len(stored.checksum_sha256) == 64
        assert stored.width == 400
        assert stored.height == 320
        assert stored.storage_path == prepared.storage_path
        assert stored.byte_size == len(file_bytes)
        assert file_bytes not in stored.storage_path.encode()

        queried = session.execute(text("SELECT * FROM images")).mappings().one()
        for column_name, value in queried.items():
            assert not isinstance(value, (bytes, bytearray, memoryview)), column_name
            if isinstance(value, str):
                assert "exif" not in value.lower()
                assert file_bytes.hex() not in value
                assert value != file_bytes.decode("latin1", errors="ignore")
    finally:
        session.close()
        engine.dispose()

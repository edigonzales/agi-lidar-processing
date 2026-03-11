#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

WATER_CLASSIFICATION = 9
COUNT_NODATA = -9999
FINAL_MASK_NODATA = 255
CONNECTIVITY_FLAG = "-8"
DEFAULT_OUTPUT_CRS = "EPSG:2056"
STATUS_PROCESSED = "processed"
STATUS_SKIPPED_EXISTING = "skipped_existing"
STATUS_NO_WATER = "no_water"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class TileBounds:
    minx: float
    maxx: float
    miny: float
    maxy: float
    srs: str | None = None

    def width(self, resolution: float) -> int:
        return _grid_size(self.maxx - self.minx, resolution)

    def height(self, resolution: float) -> int:
        return _grid_size(self.maxy - self.miny, resolution)


@dataclass
class RunStats:
    processed: int = 0
    skipped_existing: int = 0
    no_water: int = 0
    failed: int = 0

    def add_status(self, status: str) -> None:
        if status == STATUS_PROCESSED:
            self.processed += 1
        elif status == STATUS_SKIPPED_EXISTING:
            self.skipped_existing += 1
        elif status == STATUS_NO_WATER:
            self.no_water += 1
        elif status == STATUS_FAILED:
            self.failed += 1
        else:
            raise ValueError(f"Unbekannter Status: {status}")

    def merge(self, other: "RunStats") -> None:
        self.processed += other.processed
        self.skipped_existing += other.skipped_existing
        self.no_water += other.no_water
        self.failed += other.failed


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Verarbeitet SwissSurface3D-Kacheln zu Wasser-Masken-GeoTIFFs."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=base_dir / "ch.swisstopo.swisssurface3d-klX6c3Ot.csv",
        help="CSV-Datei mit den LAS-ZIP-URLs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=base_dir / "output" / "water_masks",
        help="Zielordner fuer finale GeoTIFFs und tiles_without_water.csv.",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=base_dir / "tmp",
        help="Arbeitsordner fuer Download-, Entpack- und Zwischenprodukte.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=base_dir / "process_water_tiles.log",
        help="Log-Datei mit Zeitstempel.",
    )
    parser.add_argument(
        "--resolution",
        type=float,
        default=0.5,
        help="Rasteraufloesung in Metern. Standard: 0.5",
    )
    parser.add_argument(
        "--sieve-threshold-px",
        type=int,
        default=25,
        help="Maximale Groesse kleiner Loecher in Pixeln, die aufgefuellt werden.",
    )
    return parser.parse_args()


def configure_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("process_water_tiles")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


def ensure_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"Fehlende Kommandos im PATH: {joined}. "
            "Bitte die conda-Umgebung 'lidar' aktivieren."
        )


def iter_urls(csv_path: Path) -> Iterable[str]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue
            url = row[0].strip()
            if not url or url.startswith("#"):
                continue
            yield url


def safe_token(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


def tile_id_from_name(name: str) -> str:
    tile_id = name
    for suffix in (".zip", ".las", ".laz"):
        if tile_id.lower().endswith(suffix):
            tile_id = tile_id[: -len(suffix)]
    return tile_id


def tile_id_from_path(path: Path) -> str:
    return tile_id_from_name(path.name)


def final_mask_path_for_tile_id(output_dir: Path, tile_id: str) -> Path:
    return output_dir / f"{tile_id}_water_mask.tif"


def final_mask_path_for_url(output_dir: Path, url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    archive_name = Path(parsed.path).name or "tile.zip"
    return final_mask_path_for_tile_id(output_dir, tile_id_from_name(archive_name))


def has_valid_final_result(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def run_command(
    command: list[str],
    logger: logging.Logger,
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    logger.info("Schritt: %s", " ".join(command))
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "Kein Fehlertext verfuegbar."
        raise RuntimeError(f"Kommando fehlgeschlagen: {' '.join(command)} | {message}")
    return completed


def run_pdal_pipeline(
    pipeline: list[Any],
    logger: logging.Logger,
    *,
    metadata_path: Path | None = None,
) -> dict[str, Any] | None:
    command = ["pdal", "pipeline", "--stdin"]
    if metadata_path is not None:
        command.extend(["--metadata", str(metadata_path)])
    run_command(command, logger, input_text=json.dumps(pipeline))
    if metadata_path is None:
        return None
    with metadata_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def collect_key_values(node: Any, key: str) -> list[Any]:
    values: list[Any] = []
    if isinstance(node, dict):
        if key in node:
            values.append(node[key])
        for value in node.values():
            values.extend(collect_key_values(value, key))
    elif isinstance(node, list):
        for value in node:
            values.extend(collect_key_values(value, key))
    return values


def find_bounds(node: Any) -> TileBounds:
    if isinstance(node, dict):
        keys = {"minx", "maxx", "miny", "maxy"}
        if keys.issubset(node):
            srs = None
            for candidate in ("comp_spatialreference", "prettywkt", "wkt", "projjson"):
                value = node.get(candidate)
                if isinstance(value, str) and value.strip():
                    srs = value.strip()
                    break
            return TileBounds(
                minx=float(node["minx"]),
                maxx=float(node["maxx"]),
                miny=float(node["miny"]),
                maxy=float(node["maxy"]),
                srs=srs,
            )
        for value in node.values():
            try:
                return find_bounds(value)
            except ValueError:
                continue
    elif isinstance(node, list):
        for value in node:
            try:
                return find_bounds(value)
            except ValueError:
                continue
    raise ValueError("Bounds konnten im PDAL-Output nicht gefunden werden.")


def _grid_size(span: float, resolution: float) -> int:
    cells = span / resolution
    rounded = round(cells)
    if math.isclose(cells, rounded, rel_tol=0.0, abs_tol=1e-6):
        return max(1, int(rounded))
    return max(1, int(math.ceil(cells - 1e-9)))


def summarize_srs(srs: str | None) -> str | None:
    if not srs:
        return None
    marker = 'AUTHORITY["EPSG","'
    if marker in srs:
        code = srs.rsplit(marker, 1)[-1].split('"', 1)[0]
        return f"EPSG:{code}"
    return srs[:80] + "..." if len(srs) > 80 else srs


def get_tile_bounds(las_path: Path, logger: logging.Logger) -> TileBounds:
    completed = run_command(["pdal", "info", "--summary", str(las_path)], logger)
    payload = json.loads(completed.stdout)
    bounds = find_bounds(payload)

    srs_candidates = collect_key_values(payload, "comp_spatialreference")
    if not srs_candidates:
        srs_candidates = collect_key_values(payload, "prettywkt")
    srs = next(
        (candidate.strip() for candidate in srs_candidates if isinstance(candidate, str) and candidate.strip()),
        None,
    )
    if srs and not bounds.srs:
        return TileBounds(bounds.minx, bounds.maxx, bounds.miny, bounds.maxy, srs)
    return bounds


def count_water_points(las_path: Path, work_dir: Path, logger: logging.Logger) -> int:
    metadata_path = work_dir / f"{tile_id_from_path(las_path)}_count_metadata.json"
    pipeline = [
        str(las_path),
        {
            "type": "filters.expression",
            "expression": f"Classification == {WATER_CLASSIFICATION}",
        },
        {"type": "filters.info"},
        {"type": "writers.null"},
    ]
    metadata = run_pdal_pipeline(pipeline, logger, metadata_path=metadata_path)
    candidates = [
        value
        for value in collect_key_values(metadata or {}, "num_points")
        if isinstance(value, (int, float))
    ]
    return int(candidates[-1]) if candidates else 0


def filter_water_points(
    las_path: Path,
    water_path: Path,
    logger: logging.Logger,
) -> None:
    pipeline = [
        str(las_path),
        {
            "type": "filters.expression",
            "expression": f"Classification == {WATER_CLASSIFICATION}",
        },
        {
            "type": "writers.las",
            "filename": str(water_path),
        },
    ]
    run_pdal_pipeline(pipeline, logger)


def rasterize_water_points(
    water_path: Path,
    count_raster_path: Path,
    bounds: TileBounds,
    resolution: float,
    logger: logging.Logger,
) -> None:
    pipeline = [
        str(water_path),
        {
            "type": "writers.gdal",
            "filename": str(count_raster_path),
            "gdaldriver": "GTiff",
            "resolution": resolution,
            "origin_x": bounds.minx,
            "origin_y": bounds.miny,
            "width": bounds.width(resolution),
            "height": bounds.height(resolution),
            "output_type": "count",
            "nodata": COUNT_NODATA,
        },
    ]
    run_pdal_pipeline(pipeline, logger)


def create_binary_mask(
    count_raster_path: Path,
    raw_mask_path: Path,
    logger: logging.Logger,
) -> None:
    run_command(
        [
            "gdal_calc.py",
            "-A",
            str(count_raster_path),
            "--outfile",
            str(raw_mask_path),
            "--calc",
            "1*(A>0)",
            "--type",
            "Byte",
            "--NoDataValue",
            str(FINAL_MASK_NODATA),
            "--hideNoData",
            "--overwrite",
            "--quiet",
        ],
        logger,
    )


def fill_small_holes(
    raw_mask_path: Path,
    inverse_mask_path: Path,
    sieved_inverse_path: Path,
    final_mask_path: Path,
    sieve_threshold_px: int,
    logger: logging.Logger,
) -> None:
    run_command(
        [
            "gdal_calc.py",
            "-A",
            str(raw_mask_path),
            "--outfile",
            str(inverse_mask_path),
            "--calc",
            "1-A",
            "--type",
            "Byte",
            "--NoDataValue",
            str(FINAL_MASK_NODATA),
            "--overwrite",
            "--quiet",
        ],
        logger,
    )

    run_command(
        [
            "gdal_sieve.py",
            "-q",
            CONNECTIVITY_FLAG,
            "-st",
            str(sieve_threshold_px),
            str(inverse_mask_path),
            str(sieved_inverse_path),
        ],
        logger,
    )

    run_command(
        [
            "gdal_calc.py",
            "-A",
            str(raw_mask_path),
            "-B",
            str(inverse_mask_path),
            "-C",
            str(sieved_inverse_path),
            "--outfile",
            str(final_mask_path),
            "--calc",
            "logical_or(A==1,logical_and(B==1,C==0))",
            "--type",
            "Byte",
            "--NoDataValue",
            str(FINAL_MASK_NODATA),
            "--creation-option",
            "COMPRESS=DEFLATE",
            "--creation-option",
            "TILED=YES",
            "--overwrite",
            "--quiet",
        ],
        logger,
    )


def assign_output_crs(
    raster_path: Path,
    source_srs: str | None,
    logger: logging.Logger,
) -> str:
    target_srs = source_srs or DEFAULT_OUTPUT_CRS
    run_command(
        [
            "gdal_edit.py",
            "-a_srs",
            target_srs,
            str(raster_path),
        ],
        logger,
    )
    return target_srs


def append_no_water_record(csv_path: Path, tile_id: str, url: str, source_file: str) -> None:
    file_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["tile_id", "url", "source_file"])
        writer.writerow([tile_id, url, source_file])


def remove_path(path: Path, logger: logging.Logger, label: str) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    logger.info("Aufraeumen: %s entfernt (%s)", label, path)


def download_file(url: str, destination: Path, logger: logging.Logger) -> None:
    logger.info("Schritt: Download %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": "agi-lidar-processing/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with destination.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Download fehlgeschlagen fuer {url}: {exc}") from exc


def extract_archive(zip_path: Path, extract_dir: Path, logger: logging.Logger) -> None:
    logger.info("Schritt: Entpacken %s", zip_path.name)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ZIP-Datei ist ungueltig: {zip_path}") from exc


def find_las_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".las", ".laz"}
    )


def process_las_tile(
    las_path: Path,
    url: str,
    args: argparse.Namespace,
    no_water_csv: Path,
    logger: logging.Logger,
) -> str:
    tile_id = tile_id_from_path(las_path)
    tile_work_dir = las_path.parent / f".work_{safe_token(tile_id)}"
    tile_work_dir.mkdir(parents=True, exist_ok=True)

    water_path = tile_work_dir / f"{tile_id}_water.laz"
    count_raster_path = tile_work_dir / f"{tile_id}_water_count.tif"
    raw_mask_path = tile_work_dir / f"{tile_id}_raw_mask.tif"
    inverse_mask_path = tile_work_dir / f"{tile_id}_inverse_mask.tif"
    sieved_inverse_path = tile_work_dir / f"{tile_id}_inverse_sieved.tif"
    final_mask_path = final_mask_path_for_tile_id(args.output_dir, tile_id)

    logger.info("Kachelstart: %s", tile_id)
    try:
        if has_valid_final_result(final_mask_path):
            logger.info(
                "Kachel uebersprungen, finales Resultat existiert bereits: %s -> %s",
                tile_id,
                final_mask_path,
            )
            return STATUS_SKIPPED_EXISTING

        bounds = get_tile_bounds(las_path, logger)
        logger.info(
            "Kachel-Bounds: minx=%.3f maxx=%.3f miny=%.3f maxy=%.3f width=%d height=%d%s",
            bounds.minx,
            bounds.maxx,
            bounds.miny,
            bounds.maxy,
            bounds.width(args.resolution),
            bounds.height(args.resolution),
            f" srs={summarize_srs(bounds.srs)}" if bounds.srs else "",
        )

        water_count = count_water_points(las_path, tile_work_dir, logger)
        logger.info("Wasserpunkte: %d", water_count)

        if water_count == 0:
            logger.warning("Keine Wasserpunkte gefunden: %s", tile_id)
            append_no_water_record(no_water_csv, tile_id, url, las_path.name)
            return STATUS_NO_WATER

        logger.info("Schritt: Wasserpunkte filtern")
        filter_water_points(las_path, water_path, logger)

        logger.info("Schritt: Count-Raster erzeugen")
        rasterize_water_points(
            water_path=water_path,
            count_raster_path=count_raster_path,
            bounds=bounds,
            resolution=args.resolution,
            logger=logger,
        )

        logger.info("Schritt: Binaere Wasser-Maske erzeugen")
        create_binary_mask(count_raster_path, raw_mask_path, logger)

        logger.info("Schritt: Kleine Loecher auffuellen")
        fill_small_holes(
            raw_mask_path=raw_mask_path,
            inverse_mask_path=inverse_mask_path,
            sieved_inverse_path=sieved_inverse_path,
            final_mask_path=final_mask_path,
            sieve_threshold_px=args.sieve_threshold_px,
            logger=logger,
        )

        assigned_srs = assign_output_crs(final_mask_path, bounds.srs, logger)
        logger.info(
            "Kachel abgeschlossen: %s -> %s (crs=%s)",
            tile_id,
            final_mask_path,
            summarize_srs(assigned_srs),
        )
        return STATUS_PROCESSED
    except Exception as exc:
        logger.error("Kachel fehlgeschlagen: %s | %s", tile_id, exc)
        return STATUS_FAILED
    finally:
        for path, label in (
            (water_path, "gefilterte Wasserpunkte"),
            (count_raster_path, "Count-Raster"),
            (raw_mask_path, "Rohmaske"),
            (inverse_mask_path, "invertierte Maske"),
            (sieved_inverse_path, "gesiebte invertierte Maske"),
            (tile_work_dir / f"{tile_id}_count_metadata.json", "PDAL-Metadaten"),
            (las_path, "Original-LAS/LAZ"),
            (tile_work_dir, "Tile-Arbeitsordner"),
        ):
            remove_path(path, logger, label)
        logger.info("Kachel-Cleanup abgeschlossen: %s", tile_id)


def process_archive(
    url: str,
    args: argparse.Namespace,
    no_water_csv: Path,
    logger: logging.Logger,
) -> RunStats:
    parsed = urllib.parse.urlparse(url)
    archive_name = Path(parsed.path).name or "tile.zip"
    archive_token = safe_token(Path(archive_name).stem)
    archive_stats = RunStats()
    expected_final_mask_path = final_mask_path_for_url(args.output_dir, url)

    if has_valid_final_result(expected_final_mask_path):
        logger.info(
            "Archiv uebersprungen, finales Resultat existiert bereits: %s",
            expected_final_mask_path,
        )
        archive_stats.add_status(STATUS_SKIPPED_EXISTING)
        return archive_stats

    with tempfile.TemporaryDirectory(dir=args.temp_dir, prefix=f"{archive_token}_") as tmp_name:
        archive_work_dir = Path(tmp_name)
        zip_path = archive_work_dir / archive_name
        extract_dir = archive_work_dir / "extracted"

        logger.info("Archivstart: %s", url)
        try:
            download_file(url, zip_path, logger)
            extract_archive(zip_path, extract_dir, logger)
            remove_path(zip_path, logger, "ZIP-Datei")

            las_files = find_las_files(extract_dir)
            if not las_files:
                logger.error("Keine LAS/LAZ-Dateien im Archiv gefunden: %s", url)
                archive_stats.add_status(STATUS_FAILED)
                return archive_stats

            for las_path in las_files:
                status = process_las_tile(
                    las_path=las_path,
                    url=url,
                    args=args,
                    no_water_csv=no_water_csv,
                    logger=logger,
                )
                archive_stats.add_status(status)
            return archive_stats
        except Exception as exc:
            logger.error("Archiv fehlgeschlagen: %s | %s", url, exc)
            archive_stats.add_status(STATUS_FAILED)
            return archive_stats
        finally:
            logger.info("Archiv-Cleanup abgeschlossen: %s", url)


def main() -> int:
    args = parse_args()
    args.csv = args.csv.resolve()
    args.output_dir = args.output_dir.resolve()
    args.temp_dir = args.temp_dir.resolve()
    args.log_file = args.log_file.resolve()

    logger = configure_logging(args.log_file)
    try:
        ensure_commands(["pdal", "gdal_calc.py", "gdal_sieve.py", "gdal_edit.py"])
    except RuntimeError as exc:
        logger.error(str(exc))
        return 1

    if args.resolution <= 0:
        logger.error("--resolution muss groesser als 0 sein.")
        return 1
    if args.sieve_threshold_px < 1:
        logger.error("--sieve-threshold-px muss mindestens 1 sein.")
        return 1
    if not args.csv.exists():
        logger.error("CSV-Datei nicht gefunden: %s", args.csv)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.temp_dir.mkdir(parents=True, exist_ok=True)
    no_water_csv = args.output_dir / "tiles_without_water.csv"

    logger.info("Skriptstart")
    logger.info("CSV: %s", args.csv)
    logger.info("Output: %s", args.output_dir)
    logger.info("Temp: %s", args.temp_dir)
    logger.info("Log: %s", args.log_file)
    logger.info("Rasteraufloesung: %.3f m", args.resolution)
    logger.info("Sieve-Schwelle: %d Pixel", args.sieve_threshold_px)

    run_stats = RunStats()
    for url in iter_urls(args.csv):
        run_stats.merge(process_archive(url, args, no_water_csv, logger))

    had_errors = run_stats.failed > 0
    logger.info(
        "Laufstatistik: processed=%d skipped_existing=%d no_water=%d failed=%d",
        run_stats.processed,
        run_stats.skipped_existing,
        run_stats.no_water,
        run_stats.failed,
    )
    logger.info("Skriptende mit Status: %s", "FEHLER" if had_errors else "OK")
    return 1 if had_errors else 0


if __name__ == "__main__":
    sys.exit(main())

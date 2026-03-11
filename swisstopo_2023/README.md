```
conda create -n lidar -c conda-forge python=3.11 gdal pdal
conda activate lidar
```

## Wasser-Masken je Kachel

Das Skript [process_water_tiles.py](/Users/stefan/sources/agi-lidar-processing/swisstopo_2023/process_water_tiles.py) verarbeitet die URLs aus [ch.swisstopo.swisssurface3d-klX6c3Ot.csv](/Users/stefan/sources/agi-lidar-processing/swisstopo_2023/ch.swisstopo.swisssurface3d-klX6c3Ot.csv) kachelweise.

- Download und Entpacken jeder `las.zip`
- Extraktion der Wasserpunkte mit `Classification == 9` via PDAL
- Erzeugung einer 50-cm-Wasser-Maske als GeoTIFF ueber die volle LAS-Kachelausdehnung
- Auffuellen kleiner Loecher innerhalb von Wasserflaechen mit `gdal_calc.py` und `gdal_sieve.py`
- Logging aller Verarbeitungsschritte und Cleanup der Zwischenprodukte nach jeder Kachel

### Aufruf

```bash
conda activate lidar
python swisstopo_2023/process_water_tiles.py
```

Optionale Parameter:

```bash
python swisstopo_2023/process_water_tiles.py \
  --csv swisstopo_2023/ch.swisstopo.swisssurface3d-klX6c3Ot.csv \
  --output-dir swisstopo_2023/output/water_masks \
  --temp-dir swisstopo_2023/tmp \
  --log-file swisstopo_2023/process_water_tiles.log \
  --resolution 0.5 \
  --sieve-threshold-px 25
```

### Ausgaben

- `output/water_masks/<tile_basename>_water_mask.tif`
- `output/water_masks/tiles_without_water.csv`
- `process_water_tiles.log`

Die finalen GeoTIFFs sind Byte-Raster mit `1 = Wasser` und `0 = kein Wasser`. Das CRS wird aus der jeweiligen LAS-Kachel uebernommen; falls dort keines vorhanden ist, wird `EPSG:2056` gesetzt. Kleine Loecher bis standardmaessig 25 Pixel werden mit 8er-Konnektivitaet aufgefuellt. Kacheln ohne Wasserpunkte werden nur im Log und in `tiles_without_water.csv` festgehalten; fuer sie wird kein GeoTIFF erzeugt.

Vorhandene finale GeoTIFFs werden bei Wiederholungsläufen nicht überschrieben. Wenn `<tile_basename>_water_mask.tif` bereits als nicht-leere Datei existiert, wird die Kachel direkt übersprungen und der Lauf mit der nächsten Kachel fortgesetzt.

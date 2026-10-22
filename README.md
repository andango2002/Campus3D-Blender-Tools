# Campus3D Blender Tools

**Blender companion add-on for Campus3D Workflow [B]**

Campus3D Blender Tools is a Blender add-on for importing and managing 3D packages exported from the QGIS plugin **GSI2Campus3D**.

It was developed as part of the Campus3D Workflow for architectural, urban planning, GIS, and disaster-prevention education.

**Est. 2026 | Powered by IINO Lab.**

---

## Overview / 概要

Campus3D Blender Tools は、QGISプラグイン **GSI2Campus3D** から出力された3DデータをBlenderへ一括で読み込むためのアドオンです。

Campus3D Workflow は、次の2つのツールで構成されています。

```text
[A] QGIS
GSI2Campus3D
      ↓
Campus3D Blender Package
      ↓
[B] Blender
Campus3D Blender Tools
      ↓
GLB
```

QGIS側で作成した地域モデルをBlenderへ受け渡し、

- Terrain
- Roads
- Buildings
- Terrain texture

などをまとめて読み込み、3D地域モデルとして扱えるようにします。

---

## Main Features / 主な機能

### Campus3D package import

GSI2Campus3Dから出力されたCampus3D Blender Packageを読み込みます。

Typical package contents:

```text
Campus3D_Blender/
├── campus3d_manifest.json
├── Terrain.obj
├── Roads.obj
├── Building_Normal_Small.obj
├── Building_Normal_Large.obj
├── Building_Solid_Small.obj
├── Building_Solid_Large.obj
└── Terrain_Texture.png
```

---

### Automatic object organization

Campus3D package内のオブジェクトを自動的に読み込み、Blenderシーン内へ整理します。

Typical object groups include:

- Terrain
- Roads
- Building_Normal_Small
- Building_Normal_Large
- Building_Solid_Small
- Building_Solid_Large

---

### Terrain texture

`Terrain_Texture.png` が含まれている場合、Terrainへテクスチャを適用します。

これにより、QGIS上で使用した航空写真等を3D地形上に表示できます。

---

### Re-import workflow

Campus3D Tools is designed for repeated use during model development.

When importing a new version of the same Campus3D package, previously imported Campus3D objects can be replaced so that QGIS-side adjustments can be reflected efficiently in Blender.

Example workflow:

```text
QGIS
  ↓
Campus3D export
  ↓
Blender import
  ↓
Check model
  ↓
Return to QGIS
  ↓
Adjust settings
  ↓
Export again
  ↓
Re-import
```

---

### Building height handling

Recent versions of Campus3D Workflow separate:

- terrain vertical exaggeration
- building height

Terrain elevation may be exaggerated for visualization purposes, while building heights remain at their intended scale.

This prevents buildings from becoming unnaturally tall when terrain exaggeration is used.

---

### Water plane support

Campus3D Blender Tools can also work with water plane objects when included in the exported package.

---

### GLB workflow

Imported Campus3D scenes can be exported from Blender as GLB / glTF for reuse in:

- web-based 3D viewers
- educational systems
- portfolio systems
- VR / XR applications
- CampusMint

---

## Requirements / 必要環境

Development and testing have mainly been performed with:

```text
Blender 4.5 LTS
Windows 11
```

Compatibility with other Blender versions and operating systems is not guaranteed.

---

## Installation / インストール

### Current add-on version

Current public beta:

```text
v0.4.6
```

Download the release ZIP:

```text
Campus3D_Tools_v0.4.6_ExtensionCandidate_fixed.zip
```

Do not extract the ZIP when installing it into Blender.

In Blender:

```text
Edit
→ Preferences
→ Add-ons
→ Install from Disk
```

Select the ZIP file and enable the Campus3D add-on.

After installation, press:

```text
N
```

in the 3D Viewport.

The Campus3D panel will appear in the right-side sidebar.

---

## Basic Workflow / 基本的な使い方

### 1. Create a Campus3D package in QGIS

Use the companion QGIS plugin:

**GSI2Campus3D**

to generate the Blender package.

---

### 2. Open Blender

Open Blender and enable the Campus3D Tools add-on.

---

### 3. Open the Campus3D panel

In the 3D Viewport, press:

```text
N
```

and select the Campus3D tab.

---

### 4. Select the exported package

Choose the folder exported by GSI2Campus3D.

The add-on reads the package information and imports the available objects.

---

### 5. Inspect the model

The imported regional model can be examined using Blender's standard tools.

Examples:

- free viewpoint movement
- terrain observation
- relationship between buildings and topography
- road alignment
- regional scale
- stereoscopic visualization
- lighting and material adjustment

---

### 6. Export GLB

The completed model can be exported using Blender's glTF / GLB export function.

Example:

```text
File
→ Export
→ glTF 2.0
```

GLB files can then be used in other applications and web-based 3D viewers.

---

## Companion QGIS Plugin

Campus3D Blender Tools is designed to work with:

**GSI2Campus3D**

Campus3D Workflow:

```text
[A] GSI2Campus3D for QGIS
       ↓
[B] Campus3D Blender Tools
       ↓
GLB
```

The QGIS plugin handles preparation of:

- terrain
- buildings
- roads
- terrain texture
- scale
- elevation settings

while the Blender add-on handles model import and scene setup.

---

## Educational Use / 教育利用

Campus3D Workflow was developed for educational use in:

- architecture
- urban planning
- GIS
- geography
- disaster prevention

The workflow is intended to reduce repetitive technical operations so that students can spend more time observing and interpreting real regions in 3D.

Possible learning activities include:

- comparing 2D and 3D representations
- understanding terrain and urban form
- examining building locations in relation to elevation
- comparing multiple regions
- disaster-prevention analysis
- stereoscopic observation
- GLB-based portfolio creation

---

## Project Status

Current Blender add-on version:

```text
v0.4.6
```

Status:

**Public beta / educational research version**

The add-on is under active development.

Bug reports, feature requests, and reports of successful use in other environments are welcome.

---

## Author

**Akinaru Iino**  
IINO Lab.  
Niigata Institute of Technology, Japan

Campus3D Workflow  
**Est. 2026 | Powered by IINO Lab.**

---

## License

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later).

---

## Acknowledgements

Campus3D Blender Tools is part of the Campus3D Workflow and is intended to be used together with GSI2Campus3D for QGIS.

Blender is developed by the Blender Foundation and the Blender community.

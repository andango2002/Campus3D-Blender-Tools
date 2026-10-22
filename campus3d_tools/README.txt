Campus3D Tools v0.4.4

推奨ワークフローを「単一QGIS OBJから分類を推測」から、GSI2Campus3D v0.2.8が作る6ファイル構成へ変更しました。

Campus3D_Blender/
  campus3d_manifest.json
  Terrain.obj
  Roads.obj
  Building_Normal_Small.obj
  Building_Normal_Large.obj
  Building_Solid_Small.obj
  Building_Solid_Large.obj

使い方:
1. QGIS側 GSI2Campus3D v0.2.8 で Blenderパッケージを作成
2. Blender > N > Campus3D > 「Campus3Dパッケージを一括読込」
3. campus3d_manifest.json を選択

分類はファイル名/manifestで確定するため、QGIS Scene Exportで発生したNormal_Smallへの混在を回避します。
縮尺、実寸範囲、原点もmanifestから自動取得します。

従来の単一OBJ読込機能は互換用に残しています。
地図貼り、位置合わせ、リアルタイムUV調整も継続利用できます。


v0.4.1:
- 推奨パッケージ方式にはQGIS衛星画像テクスチャが含まれないことをUIに明記。
- 「QGISの衛星画像マテリアルを削除」を「旧方式OBJの既存Terrainマテリアルを削除」に改名。初期値OFF。
- manifestのvertical_scaleを読み取り、読込レポートとSceneプロパティに保存。


v0.4.3:
- Campus3D > Water コレクションを自動作成
- 「海面を追加 / 更新」「海面を削除」を追加
- 海面標高は実世界[m]で指定（既定 0.0m）
- manifest の origin.z / scale_denominator / vertical_scale を使い、
  海抜0mを正しいBlender Zへ自動変換
- 水面の広がりマージンも実世界[m]で指定（既定 200m）
- Campus3D_Water マテリアルを自動生成
- 既存Waterがある場合は重複せず更新


v0.4.4 UI Brand Edition:
- Campus3D Workflow [B] brand banner added to the N-panel.
- Unified wording with QGIS [A] GSI2Campus3D.
- Added "Est. 2026 | Powered by IINO Lab."
- Workflow identity shown as [A] QGIS -> [B] Blender -> GLB -> CampusMint.
- Functional behavior is unchanged from v0.4.3.


v0.4.5
- GSI2Campus3D v0.4.18+ の QGIS 3D互換・建物高さ仕様を認識します。
- 旧形式パッケージを Z倍率 != 1 で読み込んだ場合、建物高さ誇張の可能性を警告します。

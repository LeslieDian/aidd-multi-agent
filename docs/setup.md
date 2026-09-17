# 环境搭建（setup.md）

> 本文档补充 README 中的快速开始，给出每个工具的详细安装步骤。

---

## 1. Python 环境

```bash
conda create -n aidd python=3.10 -y
conda activate aidd
pip install -r requirements.txt
```

## 2. RDKit

RDKit 已经通过 `requirements.txt` 的 `rdkit` 包安装。可验证：

```bash
python -c "from rdkit import Chem; print(Chem.MolFromSmiles('CCO'))"
```

## 3. AutoDock Vina

### macOS / Linux（推荐）

```bash
conda install -c conda-forge vina
vina --version
```

### Windows

1. 从 https://github.com/ccsb-scripps/AutoDock-Vina/releases 下载 `vina.exe`
2. 放到 PATH 里（例如 `C:\Program Files\vina\`），或将 `vina.exe` 复制到 `tools/` 目录

### 备选：GNINA（深度学习打分）

如果 Vina 装不上，可以用 [GNINA](https://github.com/gnina/gnina) 替代，提供 CNNscore。

## 4. Meeko（SMILES → pdbqt）

```bash
pip install meeko
python -c "from meeko import MoleculePreparation; print('meeko OK')"
```

## 5. API 密钥

默认只用 MiniMax。DeepSeek provider 块在 `config.yaml` 里以注释形式保留，按需启用。

```bash
cp .env.example .env
# 编辑 .env，填入 MiniMax_API_KEY
# 可选：填入 DEEPSEEK_API_KEY 并在 config.yaml 里启用对应 provider 块
```

去这里申请：
- MiniMax: https://api.minimaxi.chat/user-center/basic-information/interface-key
- DeepSeek（可选）: https://platform.deepseek.com/

## 6. EGFR 靶点

```bash
# 第一次运行会自动下载到 data/1M17.pdb
# 也可手动下载：https://www.rcsb.org/structure/1M17
```

---

## 常见问题

**Q: Vina 报 `cannot open receptor` 怎么办？**
A: 检查 PDB 文件是否被 Meeko 工具链污染了原子命名。原始 PDB 即可。

**Q: SA score 报找不到文件？**
A: 需要从 https://github.com/rdkit/rdkit/blob/master/Contrib/SA_Score/sascorer.py 下载放到 `tools/` 下。

**Q: Meeko 报 `Charges not assigned`？**
A: 跑 `python -c "from meeko import MoleculePreparation, PDBQTWriterLegacy"`，检查 RDKit 版本 ≥ 2023.3。
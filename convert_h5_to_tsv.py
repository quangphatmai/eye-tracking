import h5py
import pandas as pd
import sys

def decode_list(arr):
    return [x.decode("utf-8") if isinstance(x, bytes) else x for x in arr]

def read_pandas_fixed_group(f, group_name):
    g = f[group_name]
    block_nums = sorted({
        int(k[5:-6]) for k in g.keys()
        if k.startswith("block") and k.endswith("_items")
    })
    dfs = []
    for n in block_nums:
        items = decode_list(g[f"block{n}_items"][:])
        values = g[f"block{n}_values"][:]
        dfs.append(pd.DataFrame(values, columns=items))
    df = pd.concat(dfs, axis=1)
    axis0 = decode_list(g["axis0"][:])
    df = df[axis0]  # sắp lại đúng thứ tự cột gốc
    df.index = g["axis1"][:]
    return df

def convert(h5_path, tsv_path):
    with h5py.File(h5_path, "r") as f:
        df = read_pandas_fixed_group(f, "gaze")

    print("Tổng số cột:", len(df.columns))
    print("Tên các cột:")
    for c in df.columns:
        print(" -", c)
    print("\n5 dòng đầu tiên:")
    print(df.head())

    df.to_csv(tsv_path, sep="\t", index=True)
    print(f"\nĐã lưu: {tsv_path}")

if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])
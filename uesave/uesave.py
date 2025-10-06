from argparse import ArgumentParser
from pathlib import Path

from uesave import *


def main():
    parser = ArgumentParser()
    parser.add_argument('--savefile', '-s', type=Path,
                        help='Path to the Unreal Engine save file')
    parser.add_argument('--compression', '-c', default='auto',
                        choices=['auto', 'none', 'zlib',
                                 'deflate', 'gzip', 'lz4', 'zstd'],
                        help='Compression method to use for payload (default: auto)')
    args = parser.parse_args()

    save_file = load_savefile(
        args.savefile,
        compression=args.compression
    )

    print("Header:")
    print("Magic:", save_file.header.get("magic", ""))
    print("Version:", save_file.header.get("version", 0))
    print("File Versions:")
    if "package_file_version" in save_file.header:
        print("  Package:", save_file.header["package_file_version"])
    print("Engine Version:")
    ev = save_file.header.get("engine_version", {})
    print(f"  {ev.get('major', 0)}.{ev.get('minor', 0)}.{ev.get('patch', 0)} "
          f"(changelist {ev.get('changelist', 0)}, branch '{ev.get('branch', '')}')")
    print("SaveGame Class Name:",
          save_file.header.get("save_game_class_name", ""))

    def print_prop(prop: Property, indent: int = 0):
        prefix = ' ' * indent
        print(f"{prefix}{prop}")
        if isinstance(prop, StructProperty):
            for f in prop.fields:
                print_prop(f, indent + 4)
        elif isinstance(prop, ArrayProperty):
            if prop.inner_type == "ByteProperty":
                print(f"{prefix}    <{len(prop)} bytes>")
            elif prop.inner_type == "StructProperty":
                for i in range(0, len(prop)):
                    print_prop(prop[i], indent + 4)
            else:
                raise NotImplementedError(
                    f"ArrayProperty of type {prop.inner_type} not implemented")

    for prop in save_file.properties:
        print_prop(prop)


if __name__ == '__main__':
    main()

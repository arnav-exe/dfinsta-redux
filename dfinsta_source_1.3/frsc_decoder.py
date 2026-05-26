# Python translation of original kotlin code
# https://github.com/badawoll/frsc-decoder/tree/main
import struct
import collections


"""
* FRSCContainer is a container for reading and writing FRSC files.
* It is a simple container that maps an integer to a string, and can be used to store
* strings that are referenced by an integer ID.
*
* The format is as follows (little endian):
*  - Header:
*      - Magic value (4 bytes): 0x43535246
*      - Number of blocks (4 bytes): number of blocks in the file. Each block contains a contiguous range of strings.
*      - String start offset (4 bytes): offset of the first string pointer in the file.
*  - Block information (12 bytes * number of blocks):
*      - First number in block (2 bytes): the first number in the block.
*      - Number of entries in block (2 bytes): the number of entries in the block.
*      - Block sequence (2 bytes): the sequence of the block.
*  - String pointers (4 bytes * number of strings): the offset of the string in the file.
*  - Strings (variable): the strings in the file, each preceded by a 2-byte length.
*/
"""

class FRSCContainer:
    MAGIC_VALUE = 0x43535246
    BLOCK_ENTRY_SIZE = 6
    HEADER_SIZE = 12

    def __init__(self, filename, charset='utf-8'):
        self.charset = charset
        with open(filename, mode="rb") as file:
            byte_data = file.read()

        if len(byte_data) < 12:
            raise ValueError("Invalid data size")

        self.data = {}

        buffer = struct.unpack('<Iii', byte_data[:12])

        if buffer[0] != self.MAGIC_VALUE:
            raise ValueError("Invalid magic value")

        num_blocks = buffer[1]
        if num_blocks < 0:
            raise ValueError("Number of blocks is negative")

        string_start_offset = buffer[2]
        if string_start_offset < 12:
            raise ValueError("Invalid string start offset")

        for i in range(num_blocks):
            ptr = i * self.BLOCK_ENTRY_SIZE + self.HEADER_SIZE
            first_number_in_block, num_entries_in_block, block_seq = struct.unpack('<hhh', byte_data[ptr:ptr+6])

            print(f"Block {i}: startOffset={first_number_in_block}, numEntries={num_entries_in_block}, seq={block_seq}")

            for j in range(num_entries_in_block):
                string_entry_pointer = struct.unpack('<I', byte_data[string_start_offset + (block_seq + j) * 4:string_start_offset + (block_seq + j + 1) * 4])[0]
                string_entry_length = struct.unpack('<h', byte_data[string_entry_pointer:string_entry_pointer + 2])[0]
                string_offset = string_entry_pointer + 2

                string = byte_data[string_offset:string_offset + string_entry_length].decode(charset)
                self.data[first_number_in_block + j] = string

    def __getitem__(self, id):
        return self.data.get(id)

    @property
    def size(self):
        return len(self.data)

    @property
    def is_empty(self):
        return len(self.data) == 0

    def __setitem__(self, id, string):
        self.data[id] = string

    def __contains__(self, id):
        return id in self.data

    def __iter__(self):
        return iter(self.data.items())

    def dump(self):
        sorted_map = collections.OrderedDict(sorted(self.data.items()))
        blocks = self.get_blocks(sorted_map.keys())

        out_buffer = bytearray(struct.pack('<Iii', self.MAGIC_VALUE, len(blocks), self.HEADER_SIZE + len(blocks) * self.BLOCK_ENTRY_SIZE))

        for block, block_size in blocks.items():
            out_buffer.extend(struct.pack('<hhh', block, block_size, sum(blocks.values())))

        acc = 0
        for entry in sorted_map.values():
            out_buffer.extend(struct.pack('<I', self.HEADER_SIZE + len(blocks) * self.BLOCK_ENTRY_SIZE + len(sorted_map) * 4 + acc))
            acc += len(entry) + 2

        for entry in sorted_map.values():
            out_buffer.extend(struct.pack('<h', len(entry)))
            out_buffer.extend(entry.encode(self.charset))

        return out_buffer

    def get_blocks(self, seq):
        blocks = {}
        prev = None
        block_start = -1

        for elem in seq:
            if prev is None or elem != prev + 1:
                block_start = elem
                blocks[block_start] = 1
            else:
                blocks[block_start] += 1
            prev = elem

        return blocks

if __name__ == '__main__':
    thing = FRSCContainer('default.frsc')
    print(thing.data)
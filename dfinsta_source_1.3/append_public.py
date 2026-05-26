public = "instagram_source/res/values/public.xml"

codes = set()
nums = {}
with open(public, 'r') as file:
    lines = file.readlines()
    for line in lines:
        if "id=" in line:
            num = line.split("id=")[1][1:11]
            short = num[0:6]
            codes.add(short)
            num = int(num, 16)
            i = line.index('type="') + 6
            j = line.index('name') - 2
            type = line[i:j]
            if type in nums:
                if nums[type] < num:
                    nums[type] = num
            else:
                nums[type] = num

thing = list(codes)
thing.sort()
print(thing)
print(nums)
del lines[-1]

last_used = thing[-1]

def get_higher_hex_prefix(prefix):
    blah = int(prefix[0:6], 16) + 1
    return hex(blah) + "0000"

print(last_used)
print(get_higher_hex_prefix(last_used))

new_public_file = "newPublic.txt"

new_lines = []
with open(new_public_file, 'r') as file:
    for line in file.readlines():
        num += 1
        i = line.index('type="') + 6
        j = line.index('name') - 2
        type = line[i:j]
        if not type in nums:
            last_used = get_higher_hex_prefix(last_used)
            nums[type] = int(last_used, 16)
        else:
            nums[type] +=1
        num = nums[type]

        i = line.index('id="') + 4
        new = line[:i] + hex(num) + line[i + 10:]
        new_lines.append(new)


lines += new_lines
lines.append("</resources>\n")

open(public, 'w').writelines(lines)

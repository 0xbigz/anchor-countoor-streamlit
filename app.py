import streamlit as st
import pandas as pd
import numpy as np
import datetime
import random
import re

EXAMPLE = """#[account]
pub struct MyData {
    pub val: u16,
    pub is_cool: bool,
    pub state: GameState,
    pub van : [f64; 5],
    pub maybe: Option<i128>
    pub players: Vec<Pubkey> // we want to support up to 10 players
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, PartialEq, Eq)]
pub enum GameState {
    Active,
    Tie,
    Won { winner: Pubkey, score: i128 },
}
"""

RULES_STR = """
| Types      | Space in bytes | Details/Example                                                   |
|------------|----------------|------------------------------------------------------------------|
| [bool]()       | 1              | would only require 1 bit but still uses 1 byte                     |
| u8/i8      | 1              |                                                                  |
| u16/i16    | 2              |                                                                  |
| u32/i32    | 4              |                                                                  |
| u64/i64    | 8              |                                                                  |
| u128/i128  | 16             |                                                                  |
| [T;amount] | space(T) * amount | e.g. space([u16;32]) = 2 * 32 = 64                             |
| Pubkey     | 32             |                                                                  |
| Vec<T>     | 4 + (space(T) * amount) | Account size is fixed so account should be initialized with sufficient space from the beginning |
| String     | 4 + length of string in bytes | Account size is fixed so account should be initialized with sufficient space from the beginning |
| Option<T>  | 1 + (space(T)) |                                                                  |
| Enum       | 1 + Largest Variant Size | e.g. Enum { A, B { val: u8 }, C { val: u16 } } -> 1 + space(u16) = 3 |
| f32        | 4              | serialization will fail for NaN                                   |
| f64        | 8              | serialization will fail for NaN                                   |
"""
DEFAULT_SIZE_MAP = {
        'bool': 1,
        'u8': 1, 'i8': 1,
        'u16': 2, 'i16': 2,
        'u32': 4, 'i32': 4,
        'u64': 8, 'i64': 8,
        'u128': 16, 'i128': 16,
        'pubkey': 32,
        'f32': 4,
        'f64': 8
    }

DEFAULT_VEC = 10
DEFAULT_STR = 1
# Helper function to split variants correctly
def split_variants(text):
    variants = []
    depth = 0
    start = 0

    for i, c in enumerate(text):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        elif c == ',' and depth == 0:
            variants.append(text[start:i].strip())
            start = i + 1

    # Append last item
    variants.append(text[start:].strip())

    return variants
class WarningContainer:
    def warning(self, message):
        print("Warning: " + message)

    def error(self, message):
        print("Error: " + message)

def calculate_struct_size(text, warning_cont, size_map=DEFAULT_SIZE_MAP, session=None):
    enum_sizes = {}
    struct_size = 0
    struct_calc_strs = ""
    comments_strs = []

    # Split the input text into different sections and ignore the 'impl' section
    sections = re.split(r'(pub struct|pub enum|impl)', text, flags=re.IGNORECASE)
        
    # Pair each identifier with its corresponding section
    sections = list(zip(sections[1::2], sections[2::2]))

    # Sort sections so 'pub enum' comes first
    sections.sort(key=lambda x: {'pub enum': 1, 'pub struct': 2, 'impl': 3}[x[0].lower()])




    for identifier, section in sections:
        section = identifier.lower() + section
        if 'impl' in section:
            continue  # Ignore 'impl' section

        # Parse enums and store their sizes
        if 'pub enum' in section:
            enum_name, enum_body = re.search(r'pub enum (.*?) {([^}]*)}', section, re.DOTALL).groups()
            enum_name = enum_name.strip()

            enum_variants = split_variants(enum_body.strip())
            
            enum_size = 1  # Minimum size of an enum is 1 for the discriminant
            enum_calc_str = "1"  # 1 for the discriminant

            for variant in enum_variants:
                variant = variant.strip()
                if '{' in variant:
                    variant_name, variant_fields = variant.split('{', 1)
                    variant_fields = variant_fields.rsplit('}', 1)[0].split(',')

                    variant_size = 0
                    variant_calc_str = []
                    for field in variant_fields:
                        field_name, field_type = field.split(':')
                        field_type = field_type.strip()
                        variant_size += size_map[field_type.lower()]
                        variant_calc_str.append(field_type.lower())

                    enum_size = max(enum_size, 1 + variant_size)
                    enum_calc_str = f"1 + {' + '.join(variant_calc_str)}"

            enum_sizes[enum_name.lower()] = (enum_size, enum_calc_str)
                
        # Parse the struct and find all lines that define a variable
        elif 'pub struct' in section:

            def safe_get_size(base_type):
                this_size_0 = 0
                if base_type.lower() in size_map:
                    this_size_0 = size_map.get(base_type.lower(), 0)
                elif base_type.lower() in enum_sizes:
                    this_size_0 = enum_sizes[base_type.lower()][0]
                else:
                    warning_cont.error('ERROR: no type "`' +  base_type + '`" in map (for "`'+str(line)+'`")')
                return this_size_0
            
            struct_calc_strs = []

            lines = section.split("\n")
            for line in lines:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                
                # Remove comments, if any
                line = line.split("//")[0]
                
                # Parse type and variable name
                parts = line.split(":")
                if len(parts) != 2:
                    continue
                type_str = parts[1].strip().rstrip(",").rstrip(";").strip()

                # Check for special types
                if type_str.lower().startswith("vec<"):
                    vec_size = st.session_state.get('vec_size', DEFAULT_VEC)
                    warning_cont.warning(f'spotted `Vec`, assuming length {vec_size}')
                    base_type = re.search(r'vec<(.*?)>', type_str, re.IGNORECASE).group(1)
                    this_size = 4 + size_map[base_type.lower()] * vec_size
                    struct_size += this_size
                    struct_calc_strs.append("4 + {} * {}".format(base_type.lower(), vec_size))
                    comments_strs.append(f"+ {this_size} // {parts[0]}: {type_str}")
                elif type_str.lower().startswith("option<"):
                    base_type = re.search(r'option<(.*?)>', type_str, re.IGNORECASE).group(1)
                    this_size_0 = safe_get_size(base_type)
                    this_size = 1 + this_size_0
                    struct_size += this_size
                    struct_calc_strs.append("1 + {}".format(base_type.lower()))
                    comments_strs.append(f"+ {this_size} // {parts[0]}: {type_str}")
                elif type_str.startswith("string"):
                    this_size = 4 + DEFAULT_STR
                    struct_size += this_size
                    struct_calc_strs.append(f"4 + {DEFAULT_STR}")
                    comments_strs.append(f"+ {this_size} // {parts[0]}: {type_str}")
                elif type_str.startswith("[") and type_str.endswith("]"):
                    match = re.search(r'\[(.*?);(.*?)\]', type_str)
                    base_type = match.group(1)
                    amount = int(match.group(2))
                    this_size_0 = safe_get_size(base_type)
                    this_size = this_size_0  * amount
                    struct_size += this_size
                    struct_calc_strs.append("{} * {}".format(base_type.lower(), amount))
                    comments_strs.append(f"+ {this_size} // {parts[0]}: {type_str}")
                elif type_str.lower() in enum_sizes:
                    enum_size, enum_calc_str = enum_sizes[type_str.lower()]
                    struct_size += enum_size
                    struct_calc_strs.append(enum_calc_str)
                    comments_strs.append(f"+ {enum_size} // {parts[0]}: {type_str}")
                elif type_str.lower() in size_map:
                    struct_size += size_map[type_str.lower()]
                    struct_calc_strs.append(type_str.lower())
                    comments_strs.append(f"+ { size_map[type_str.lower()]} // {parts[0]}: {type_str}")
                else:
                    warning_cont.error('no type: "' +  type_str + '" in byte size map')

    size_map.update({key: value[0] for key,value in enum_sizes.items()})
    
    return struct_size, struct_calc_strs, comments_strs, size_map

def main():
    st.set_page_config(
        'Anchor "Countoor"',
        layout='wide',
        page_icon="⚓🧮"
    )
    st.header('Anchor "Countoor" ⚓🧮')
    st.caption("Sail through byte measurements effortlessly")
    tabs = st.tabs(['calculate size', 'byte rules'])
    
    st.session_state['code_input'] = EXAMPLE
    st.session_state['code_output_mode'] = False
    # st.sidebar.header('byte size map')

    # man_letters = st.sidebar.text_input('manual override letters:', '')
    struct = None
    size = None
    comments = None

    with tabs[0]:
        c1, c2 = st.columns(2)
        # mode = c1.radio('', ['input', 'code'], horizontal=True)
        # tabs2 = c1.tabs(['input', 'code'])
        b1, _, b2, _ = st.columns([2,1,1,3], gap='small')
        if b2.button('Clear'):
            st.session_state.clear()
        if b1.button('Example'):
            st.session_state.clear()
            st.session_state['code_input'] = EXAMPLE
        struct = c1.text_area("code input:", st.session_state.get('code_input', ''), height=400)

        st.session_state['code_input'] = struct
        code_cont = c2.container()
        warn_cont = c2.container()
        size, comments, comments_strs, size_map = calculate_struct_size(struct, warn_cont, DEFAULT_SIZE_MAP, st.session_state)
        noms = [x.split('{')[0].strip() for x in struct.split('struct') if '{' in x]
        noms = [nom for nom in noms if '\npub' not in nom]
        the_nom = ''.join(noms)
        if size != 0 and st.session_state.get('code_output_mode'):
            # c2.markdown(' \n\n')
            code_cont.markdown(f'**`{the_nom}`** : **`{size}`** bytes')
        else:
            code_cont.markdown(' \n\n')
            code_cont.markdown(' \n\n')

        # st.sidebar.json(size_map, expanded=True)
        # st.sidebar.write(comments_strs)
        # st.sidebar.write(st.session_state)

        if struct and size>0:
            code_res = ""
            if st.session_state.get('code_output_mode', False):
                code_res += struct
            
            code_res += """\n
impl """+the_nom+""" {
    pub const MAX_SIZE: usize = """+str(size)+""";
    // """ +'\n    // '.join(comments_strs+[';'])+"""
}
        """
            
            if st.session_state.get('code_output_mode', False):
                code_res += """
    #[derive(Accounts)]
    pub struct Initialize"""+the_nom+"""<'info> {
        // Note that we have to add 8 to the space for the internal anchor
        #[account(init, payer = signer, space = 8 + """+the_nom+"""::MAX_SIZE)]
        pub acc: Account<'info, """+the_nom+""">,
        pub signer: Signer<'info>,
        pub system_program: Program<'info, System>
    }"""
            code_cont.code(code_res, language="rust",)
        elif struct.strip()!='' and size==0:
            code_cont.code('// cannot detect account size', language="rust",)
        else:
            code_cont.code('// write a valid rust account', language="rust",)
    
    # with tabs[1]:
    #     st.markdown('track indexing for memory comparision [(memcpy)](https://solanacookbook.com/guides/get-program-accounts.html#memcmp)')
    #     m1, m2 = st.columns(2)
    #     val = m1.number_input(f'memcpy ({0} -> {size}):', min_value=0, max_value=size, step=1)
    #     if size > 0:
    #         val = m1.slider('', min_value=0, max_value=size, value=val, step=1, label_visibility='collapsed')
    #         # m1.progress(val/size)
    #     m1.code(' + '.join(['('+x+')' for x in comments])+' = '+ str(size))
    #     m2.code(struct, language='rust')

    with tabs[1]:
        st.write("""Note: This only applies to accounts that don't use zero-copy. zero-copy uses repr(C) with a pointer cast, so there the C layout applies.""")
        st.markdown('Call to action from [this Superteam Request](https://earn.superteam.fun/listings/bounties/build-an-anchor-space-calculator/)')
        st.markdown('This [reference](https://www.anchor-lang.com/docs/space) tells you how much space you should allocate for an account.')
        # st.write(RULES_STR)
        df = pd.DataFrame(DEFAULT_SIZE_MAP, index=['Space in bytes']).T
        df.index.name = 'Types'
        d1, d2 = st.columns(2)
        d1.dataframe(df, use_container_width=True)   
        assumps_df = pd.DataFrame([('Vec', 10), ('String', 1)], columns=['Custom Types', 'Space in bytes'])
        edited_df = d2.experimental_data_editor(assumps_df,
                                    # height=260,
                                    num_rows="dynamic", disabled=False, use_container_width=True) 
        # st.write(edited_df)
        # st.write(edited_df.set_index('Custom Types').loc['Vec', 'Space in bytes'])
        t1 = edited_df.set_index('Custom Types').loc['Vec', 'Space in bytes']
        t2 = edited_df.set_index('Custom Types').loc['String', 'Space in bytes']
        cur_str = st.session_state.get('str_size')
        cur_vec = st.session_state.get('vec_size')                                        
        if (t1 != DEFAULT_VEC and cur_vec is None) or (t1 != st.session_state.get('vec_size', None) and cur_vec is not None):
            st.session_state['vec_size'] = t1
            st.experimental_rerun()
        if (t2 != DEFAULT_STR and cur_str is None) or (t2 != st.session_state.get('str_size', None) and cur_str is not None):
            st.session_state['str_size'] = t2
            st.experimental_rerun()




        # d2.write(edited_df)
        # st.divider() 
        # st.markdown('sources: [anchor-lang documentation](https://www.anchor-lang.com/docs/space)')


if __name__ == '__main__':
    main()
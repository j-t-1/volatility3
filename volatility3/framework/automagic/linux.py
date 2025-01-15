# This file is Copyright 2019 Volatility Foundation and licensed under the Volatility Software License 1.0
# which is available at https://www.volatilityfoundation.org/license/vsl-v1.0
#

import logging
from typing import Optional, Tuple

from volatility3.framework import constants, interfaces
from volatility3.framework.automagic import symbol_cache, symbol_finder
from volatility3.framework.layers import intel, scanners
from volatility3.framework.symbols import linux

vollog = logging.getLogger(__name__)


class LinuxIntelStacker(interfaces.automagic.StackerLayerInterface):
    stack_order = 35
    exclusion_list = ["mac", "windows"]

    @classmethod
    def stack(
        cls,
        context: interfaces.context.ContextInterface,
        layer_name: str,
        progress_callback: constants.ProgressCallback = None,
    ) -> Optional[interfaces.layers.DataLayerInterface]:
        """Attempts to identify linux within this layer."""
        # Bail out by default unless we can stack properly
        layer = context.layers[layer_name]
        join = interfaces.configuration.path_join

        # Never stack on top of an intel layer
        # FIXME: Find a way to improve this check
        if isinstance(layer, intel.Intel):
            return None

        linux_banners = symbol_cache.load_cache_manager().get_identifier_dictionary(
            operating_system="linux"
        )
        # If we have no banners, don't bother scanning
        if not linux_banners:
            vollog.info(
                "No Linux banners found - if this is a linux plugin, please check your symbol files location"
            )
            return None

        mss = scanners.MultiStringScanner([x for x in linux_banners if x is not None])
        for _, banner in layer.scan(
            context=context, scanner=mss, progress_callback=progress_callback
        ):
            dtb = None
            vollog.debug(f"Identified banner: {repr(banner)}")

            isf_path = linux_banners.get(banner, None)
            if isf_path:
                table_name = context.symbol_space.free_table_name("LintelStacker")
                table = linux.LinuxKernelIntermedSymbols(
                    context,
                    "temporary." + table_name,
                    name=table_name,
                    isf_url=isf_path,
                )
                context.symbol_space.append(table)
                kaslr_shift, aslr_shift = cls.find_aslr(
                    context, table_name, layer_name, progress_callback=progress_callback
                )

                if "init_top_pgt" in table.symbols:
                    layer_class = intel.LinuxIntel32e
                    dtb_symbol_name = "init_top_pgt"
                elif "init_level4_pgt" in table.symbols:
                    layer_class = intel.LinuxIntel32e
                    dtb_symbol_name = "init_level4_pgt"
                elif (
                    "pkmap_count" in table.symbols
                    and table.get_symbol("pkmap_count").type.count == 512
                ):
                    layer_class = intel.LinuxIntelPAE
                    dtb_symbol_name = "swapper_pg_dir"
                else:
                    layer_class = intel.LinuxIntel
                    dtb_symbol_name = "swapper_pg_dir"

                dtb = cls.virtual_to_physical_address(
                    table.get_symbol(dtb_symbol_name).address + kaslr_shift
                )

                # Build the new layer
                new_layer_name = context.layers.free_layer_name("IntelLayer")
                config_path = join("IntelHelper", new_layer_name)
                context.config[join(config_path, "memory_layer")] = layer_name
                context.config[join(config_path, "page_map_offset")] = dtb
                context.config[
                    join(config_path, LinuxSymbolFinder.banner_config_key)
                ] = str(banner, "latin-1")

                layer = layer_class(
                    context,
                    config_path=config_path,
                    name=new_layer_name,
                    metadata={"os": "Linux"},
                )
                layer.config["kernel_virtual_offset"] = aslr_shift

            if layer and dtb:
                vollog.debug(f"DTB was found at: 0x{dtb:0x}")
                return layer
        vollog.debug("No suitable linux banner could be matched")
        return None

    @classmethod
    def find_aslr(
        cls,
        context: interfaces.context.ContextInterface,
        symbol_table: str,
        layer_name: str,
        progress_callback: constants.ProgressCallback = None,
    ) -> Tuple[int, int]:
        """Determines the offset of the actual DTB in physical space and its
        symbol offset."""
        init_task_symbol = symbol_table + constants.BANG + "init_task"
        init_task_json_address = context.symbol_space.get_symbol(
            init_task_symbol
        ).address
        swapper_signature = rb"swapper(\/0|\x00\x00)\x00\x00\x00\x00\x00\x00"
        module = context.module(symbol_table, layer_name, 0)
        address_mask = context.symbol_space[symbol_table].config.get(
            "symbol_mask", None
        )

        task_symbol = module.get_type("task_struct")
        comm_child_offset = task_symbol.relative_child_offset("comm")

        for offset in context.layers[layer_name].scan(
            scanner=scanners.RegExScanner(swapper_signature),
            context=context,
            progress_callback=progress_callback,
        ):
            init_task_address = offset - comm_child_offset
            init_task = module.object(
                object_type="task_struct", offset=init_task_address, absolute=True
            )
            if init_task.pid != 0:
                continue
            elif (
                init_task.has_member("state")
                and init_task.state.cast("unsigned int") != 0
            ):
                continue
            elif init_task.active_mm.cast("long unsigned int") == module.get_symbol(
                "init_mm"
            ).address and init_task.tasks.next.cast(
                "long unsigned int"
            ) == init_task.tasks.prev.cast(
                "long unsigned int"
            ):
                # The idle task steals `mm` from previously running task, i.e.,
                # `init_mm` is only used as long as no CPU has ever been idle.
                # This catches cases where we found a fragment of the
                # unrelocated ELF file instead of the running kernel.
                continue

            # This we get for free
            aslr_shift = (
                int.from_bytes(
                    init_task.files.cast("bytes", length=init_task.files.vol.size),
                    byteorder=init_task.files.vol.data_format.byteorder,
                )
                - module.get_symbol("init_files").address
            )
            kaslr_shift = init_task_address - cls.virtual_to_physical_address(
                init_task_json_address
            )
            if address_mask:
                aslr_shift = aslr_shift & address_mask

            if aslr_shift & 0xFFF != 0 or kaslr_shift & 0xFFF != 0:
                continue
            vollog.debug(
                f"Linux ASLR shift values determined: physical {kaslr_shift:0x} virtual {aslr_shift:0x}"
            )
            return kaslr_shift, aslr_shift

        # We don't throw an exception, because we may legitimately not have an ASLR shift, but we report it
        vollog.debug("Scanners could not determine any ASLR shifts, using 0 for both")
        return 0, 0

    @classmethod
    def virtual_to_physical_address(cls, addr: int) -> int:
        """Converts a virtual linux address to a physical one (does not account
        of ASLR)"""
        if addr > 0xFFFFFFFF80000000:
            return addr - 0xFFFFFFFF80000000
        return addr - 0xC0000000


class LinuxSymbolFinder(symbol_finder.SymbolFinder):
    """Linux symbol loader based on uname signature strings."""

    banner_config_key = "kernel_banner"
    operating_system = "linux"
    symbol_class = "volatility3.framework.symbols.linux.LinuxKernelIntermedSymbols"
    exclusion_list = ["mac", "windows"]

    @classmethod
    def find_aslr(cls, *args):
        return LinuxIntelStacker.find_aslr(*args)[1]

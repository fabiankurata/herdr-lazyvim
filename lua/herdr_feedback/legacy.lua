return dofile(debug.getinfo(1, "S").source:sub(2):gsub("/lua/herdr_feedback/legacy.lua$", "/nvim/lua/herdr_feedback/legacy.lua"))

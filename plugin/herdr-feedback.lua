return dofile(debug.getinfo(1, "S").source:sub(2):gsub("/plugin/herdr%-feedback.lua$", "/nvim/plugin/herdr-feedback.lua"))
